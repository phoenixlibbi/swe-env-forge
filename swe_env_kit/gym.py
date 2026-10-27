"""An RL-style environment over a task sandbox, with SWE-bench Pro style budgets.

``SWEEnv.reset()`` opens a sandbox at the base commit (future history stripped,
hidden paths removed) and returns the agent-visible task. ``step(action)`` runs one
action and returns ``(observation, done, info)``. Supported actions::

    {"type": "bash", "command": "pytest -x tests/"}
    {"type": "write_file", "path": "pkg/x.py", "content": "..."}
    {"type": "submit"}

The working directory persists between bash steps (``cd`` works); other shell state
does not. Budgets: turns, agent-reported cost (USD) and wall-clock time. Every
``checkpoint_every`` turns the current diff is saved, so progress can be graded per turn.
"""

from __future__ import annotations

import concurrent.futures as cf
import hashlib
import shlex
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .grade import grade
from .sandbox import Sandbox
from .task import Task, append_jsonl
from .trajectory import Checkpoint, Episode, Step
from .workspace import collect_diff, open_sandbox, prepare, shell_prefix

INSTRUCTIONS = """You are working in a git repository at {workdir}. Resolve the task described below by editing the
code. Each turn, run ONE bash command. The working directory persists between commands; environment
variables and shell functions do not. Do not modify or delete existing tests unless the task requires it.
When you are done, submit. Your changes (the git diff against the starting commit) are your answer and are
graded by hidden tests."""


@dataclass
class Budget:
    max_turns: int = 50
    max_cost: float | None = None        # USD, as reported by the agent
    max_seconds: float | None = None     # wall clock for the whole episode
    command_timeout: float = 180.0
    max_output_chars: int = 12_000


def truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = limit * 2 // 3, limit // 3
    return f"{text[:head]}\n... [{len(text) - head - tail} characters truncated] ...\n{text[-tail:]}"


class SWEEnv:
    def __init__(self, task: Task, backend: str = "docker", budget: Budget | None = None, *,
                 network: bool = False, checkpoint_every: int = 0, include_hints: bool = False, keep: bool = False):
        self.task, self.backend, self.budget = task, backend, budget or Budget()
        self.network, self.checkpoint_every = network, checkpoint_every
        self.include_hints, self.keep = include_hints, keep
        self.sb: Sandbox | None = None
        self.turn, self.cost, self.started = 0, 0.0, 0.0
        self.done, self.exit_reason = False, ""
        self.steps: list[Step] = []
        self.checkpoints: list[Checkpoint] = []
        self.final_patch = ""

    # -------------------------------------------------------------- lifecycle
    def reset(self) -> dict[str, Any]:
        self.close()
        self.sb = open_sandbox(self.task, self.backend, network=self.network, keep=self.keep)
        prepare(self.sb, self.task, for_agent=True)
        self.sb.write_file(f"{self.sb.scratch}/cwd", self.sb.workdir + "\n")
        self.turn, self.cost, self.started = 0, 0.0, time.monotonic()
        self.done, self.exit_reason, self.steps, self.checkpoints, self.final_patch = False, "", [], [], ""
        return {"task": self.task.agent_view(include_hints=self.include_hints), "workdir": self.sb.workdir,
                "instructions": INSTRUCTIONS.format(workdir=self.sb.workdir), "budget": vars(self.budget)}

    def close(self) -> None:
        if self.sb is not None:
            self.sb.close()
            self.sb = None

    def __enter__(self) -> "SWEEnv":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------------- step
    def _bash(self, command: str) -> tuple[str, int, float, bool]:
        sb = self.sb
        cwd_file = f"{sb.scratch}/cwd"
        prefix = [ln for ln in shell_prefix(self.task, sb) if not ln.startswith("cd ")]
        script = "\n".join([*prefix, f'cd "$(cat {shlex.quote(cwd_file)} 2>/dev/null)" 2>/dev/null || cd {shlex.quote(sb.workdir)}',
                            command, "__swekit_rc=$?", f"pwd > {shlex.quote(cwd_file)}", "exit $__swekit_rc"])
        path = f"{sb.scratch}/step.sh"
        sb.write_file(path, script)
        limit = self.budget.command_timeout
        if self.budget.max_seconds is not None:  # never run past the episode's wall-clock budget
            limit = max(1.0, min(limit, self.budget.max_seconds - (time.monotonic() - self.started)))
        res = sb.exec(f"bash {shlex.quote(path)} 2>&1", timeout=limit)
        out = res.output
        if res.timed_out:
            out += f"\n[command timed out after {limit:.0f}s]"
        return truncate(out, self.budget.max_output_chars), res.exit_code, res.duration, res.timed_out

    def step(self, action: dict[str, Any]) -> tuple[str, bool, dict[str, Any]]:
        if self.sb is None:
            raise RuntimeError("call reset() first")
        if self.done:
            return "episode is over", True, {"exit_reason": self.exit_reason}
        kind = action.get("type")
        self.cost += float(action.get("cost") or 0.0)
        if kind == "submit":
            self._finish("submitted")
            return "submitted", True, {"exit_reason": self.exit_reason}
        self.turn += 1
        step = Step(turn=self.turn, action={k: v for k, v in action.items() if k not in ("cost", "thought")},
                    cost=float(action.get("cost") or 0.0), thought=str(action.get("thought") or ""))
        if kind == "bash":
            step.output, step.exit_code, step.duration, step.timed_out = self._bash(str(action.get("command", "")))
        elif kind == "write_file":
            try:
                self.sb.write_file(str(action["path"]), str(action.get("content", "")))
                step.output = f"wrote {action['path']}"
            except Exception as exc:  # report to the agent, do not crash the episode
                step.output, step.exit_code = f"write failed: {exc}", 1
        else:
            step.output, step.exit_code = f"unknown action type {kind!r}; use bash, write_file or submit", 2
        self.steps.append(step)
        if self.checkpoint_every and self.turn % self.checkpoint_every == 0:
            self.checkpoints.append(Checkpoint(self.turn, collect_diff(self.sb, self.task.base_commit)))
        reason = self._over_budget()
        if reason:
            self._finish(reason)
        return step.output, self.done, {"exit_reason": self.exit_reason, "turn": self.turn, "cost": self.cost}

    def _over_budget(self) -> str:
        b = self.budget
        if b.max_turns and self.turn >= b.max_turns:
            return "max_turns"
        if b.max_cost is not None and self.cost >= b.max_cost:
            return "max_cost"
        if b.max_seconds is not None and time.monotonic() - self.started >= b.max_seconds:
            return "max_time"
        return ""

    def _finish(self, reason: str) -> None:
        self.done, self.exit_reason = True, reason
        self.final_patch = collect_diff(self.sb, self.task.base_commit)
        if self.checkpoint_every and (not self.checkpoints or self.checkpoints[-1].turn != self.turn):
            self.checkpoints.append(Checkpoint(self.turn, self.final_patch))


# --------------------------------------------------------------------- episodes
def run_episode(task: Task, agent, backend: str = "docker", budget: Budget | None = None, *,
                out_dir: str | Path | None = None, do_grade: bool = True, grade_checkpoints: bool = False,
                checkpoint_every: int = 0, network: bool = False, strict: bool = True,
                timeout: float | None = None) -> Episode:
    """Run ``agent`` on ``task`` until it submits or a budget runs out, then grade in a fresh sandbox."""
    agent_name = getattr(agent, "name", type(agent).__name__)
    ep = Episode(task.instance_id, agent_name)
    start = time.monotonic()
    env = SWEEnv(task, backend, budget, network=network, checkpoint_every=checkpoint_every)
    try:
        obs = env.reset()
        agent.reset(obs)
        last: str | None = None
        while not env.done:
            try:
                action = agent.act(last)
            except Exception as exc:
                reason = "context_overflow" if "context_overflow" in str(exc) else "agent_error"
                ep.error = f"{type(exc).__name__}: {exc}"
                env._finish(reason)
                break
            last, _done, _info = env.step(action)
        ep.exit_reason, ep.turns, ep.cost = env.exit_reason, env.turn, round(env.cost, 6)
        ep.patch, ep.steps, ep.checkpoints = env.final_patch, env.steps, env.checkpoints
    except Exception as exc:
        ep.exit_reason, ep.error = "env_error", f"{type(exc).__name__}: {exc}"
        ep.steps, ep.turns = env.steps, env.turn
    finally:
        env.close()
    ep.duration = round(time.monotonic() - start, 2)
    if do_grade and ep.exit_reason != "env_error":
        ep.grade = grade(task, ep.patch, backend, strict=strict, timeout=timeout).to_dict()
        if grade_checkpoints:
            cache: dict[str, dict] = {}
            for cp in ep.checkpoints:
                key = hashlib.sha1(cp.patch.encode()).hexdigest()
                if key not in cache:
                    cache[key] = ep.grade if cp.patch == ep.patch else grade(
                        task, cp.patch, backend, strict=strict, timeout=timeout).to_dict()
                g = cache[key]
                cp.grade = {"resolved": g["resolved"], "dense": g["dense"], "failure": g["failure"]}
    if out_dir:
        ep.save(out_dir)
    return ep


def run_batch(tasks: list[Task], agent_factory: Callable[[Task], Any], backend: str = "docker", *,
              budget: Budget | None = None, out_dir: str | Path = "runs", workers: int = 1,
              model_name: str = "agent", **kw) -> list[Episode]:
    """Run many tasks (optionally in parallel). Writes per-task folders plus ``results.jsonl`` and
    ``predictions.jsonl`` (SWE-bench format, usable with the official harness)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    done = {p.parent.name for p in out.glob("*/episode.json")}
    todo = [t for t in tasks if t.instance_id not in done]
    results: list[Episode] = []

    def one(t: Task) -> Episode:
        try:
            agent = agent_factory(t)
        except Exception as exc:  # e.g. a missing replay file: record it, keep the batch going
            ep = Episode(t.instance_id, "unknown", exit_reason="agent_error", error=f"{type(exc).__name__}: {exc}")
            ep.save(out)
            return ep
        return run_episode(t, agent, backend, budget, out_dir=out, **kw)

    with cf.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for ep in pool.map(one, todo):
            results.append(ep)
            append_jsonl(out / "results.jsonl", ep.summary())
            append_jsonl(out / "predictions.jsonl", {"instance_id": ep.instance_id,
                                                     "model_name_or_path": model_name, "model_patch": ep.patch})
    return results
