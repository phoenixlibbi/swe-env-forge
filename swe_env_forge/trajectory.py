"""Episode records: one agent attempt at one task (steps, budget use, patch, grades)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

EXIT_REASONS = ("submitted", "max_turns", "max_cost", "max_time", "context_overflow", "agent_error",
                "agent_broke_env", "env_error")


@dataclass
class Step:
    turn: int
    action: dict[str, Any]
    output: str = ""
    exit_code: int = 0
    duration: float = 0.0
    timed_out: bool = False
    cost: float = 0.0
    thought: str = ""


@dataclass
class Checkpoint:
    turn: int
    patch: str
    grade: dict[str, Any] | None = None


@dataclass
class Episode:
    instance_id: str
    agent: str
    exit_reason: str = ""
    turns: int = 0
    cost: float = 0.0
    duration: float = 0.0
    patch: str = ""
    steps: list[Step] = field(default_factory=list)
    checkpoints: list[Checkpoint] = field(default_factory=list)
    grade: dict[str, Any] | None = None
    error: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def tool_error_rate(self) -> float:
        bash = [s for s in self.steps if s.action.get("type") == "bash"]
        return sum(1 for s in bash if s.exit_code != 0) / len(bash) if bash else 0.0

    def summary(self) -> dict[str, Any]:
        """Everything except the step-by-step log (kept in the trajectory file)."""
        d = asdict(self)
        d.pop("steps")
        d["tool_error_rate"] = round(self.tool_error_rate, 4)
        d["n_steps"] = len(self.steps)
        return d

    def save(self, out_dir: str | Path, unique: bool = False, name: str | None = None) -> Path:
        """Write the episode to ``out_dir/<instance_id>``.

        With ``unique``, a second episode of the same task (another attempt, another turn of a
        multi-turn run) goes to ``<instance_id>@2``, ``@3``, ... instead of overwriting the first.
        """
        out = Path(out_dir) / (name or self.instance_id)
        if unique and not name:
            k = 1
            while (out / "episode.json").exists():
                k += 1
                out = Path(out_dir) / f"{self.instance_id}@{k}"
            if k > 1:
                self.metadata["attempt"] = k
        out.mkdir(parents=True, exist_ok=True)
        with (out / "trajectory.jsonl").open("w", encoding="utf-8") as fh:
            for s in self.steps:
                fh.write(json.dumps(asdict(s), ensure_ascii=False) + "\n")
        (out / "episode.json").write_text(json.dumps(self.summary(), indent=2, ensure_ascii=False), encoding="utf-8")
        (out / "patch.diff").write_text(self.patch, encoding="utf-8")
        return out

    @classmethod
    def load(cls, path: str | Path) -> "Episode":
        """Load from an episode directory (``episode.json`` + ``trajectory.jsonl``)."""
        path = Path(path)
        data = json.loads((path / "episode.json").read_text(encoding="utf-8"))
        for k in ("tool_error_rate", "n_steps"):
            data.pop(k, None)
        data["checkpoints"] = [Checkpoint(**c) for c in data.get("checkpoints", [])]
        ep = cls(**data)
        traj = path / "trajectory.jsonl"
        if traj.exists():
            ep.steps = [Step(**json.loads(ln)) for ln in traj.read_text(encoding="utf-8").splitlines() if ln.strip()]
        return ep


def load_episodes(root: str | Path) -> list[Episode]:
    root = Path(root)
    return [Episode.load(p.parent) for p in sorted(root.glob("*/episode.json"))]


# ------------------------------------------------------------ SWE-agent import
_SWE_AGENT_EXIT = {"submitted": "submitted", "exit_cost": "max_cost", "exit_context": "context_overflow",
                   "exit_format": "agent_error", "exit_error": "agent_error", "exit_api": "agent_error",
                   "exit_command_timeout": "max_time", "exit_total_execution_time": "max_time",
                   "exit_forfeit": "submitted"}


def from_swe_agent(path: str | Path, instance_id: str | None = None) -> Episode:
    """Convert a SWE-agent ``.traj`` file into an :class:`Episode` (actions, outputs, cost, submission)."""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    info = data.get("info") or {}
    stats = info.get("model_stats") or {}
    steps = [Step(turn=i + 1, action={"type": "bash", "command": s.get("action", "")},
                  output=s.get("observation") or "", duration=float(s.get("execution_time") or 0.0),
                  thought=s.get("thought") or "")
             for i, s in enumerate(data.get("trajectory") or [])]
    raw_exit = str(info.get("exit_status") or "")
    exit_reason = next((v for k, v in _SWE_AGENT_EXIT.items() if raw_exit.startswith(k)), raw_exit or "agent_error")
    if "submitted" in raw_exit:
        exit_reason = "submitted"
    iid = instance_id or path.stem
    return Episode(instance_id=iid, agent=f"swe-agent:{(info.get('swe_agent_version') or '?')}",
                   exit_reason=exit_reason, turns=len(steps), cost=float(stats.get("instance_cost") or 0.0),
                   duration=round(sum(s.duration for s in steps), 2), patch=info.get("submission") or "",
                   steps=steps, metadata={"imported_from": "swe-agent", "raw_exit_status": raw_exit,
                                          "api_calls": stats.get("api_calls"), "tokens_sent": stats.get("tokens_sent"),
                                          "tokens_received": stats.get("tokens_received")})
