"""The task schema shared by every stage, plus JSON / JSONL helpers.

A :class:`Task` holds the agent-visible text (problem statement, requirements,
interface), the hidden material (gold patch, test patch, F2P / P2P test lists)
and an :class:`EnvSpec` describing how to build the environment and run tests.
"""

from __future__ import annotations

import dataclasses
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

SOURCES = ("swebench", "swebench-pro", "mined-pr", "mined-commits", "custom")

# How the {test_files} placeholder of a test command is rendered.
TEST_FILE_FORMATS = ("path", "dotted-module", "django", "comma", "go-package", "none")


@dataclass
class EnvSpec:
    """How to build the environment and run the tests of one task."""

    image: str | None = None
    dockerfile: str | None = None
    platform: str | None = None
    workdir: str = "/testbed"
    setup: list[str] = field(default_factory=list)
    pre_test: list[str] = field(default_factory=list)
    test_command: str | None = None
    test_files: list[str] = field(default_factory=list)
    test_file_format: str = "path"
    log_parser: str = "auto"
    run_script: str | None = None
    parser_script: str | None = None
    env_vars: dict[str, str] = field(default_factory=dict)
    timeout: int = 1800
    # Paths inside the environment that hold hidden material (for example a test
    # patch baked into the image). They are deleted before an agent starts.
    hidden_paths: list[str] = field(default_factory=list)
    # Where a sandbox starts: "base" (the task's base_commit) or "image-head", the commit the
    # image's repository is at, which must descend from base_commit. Official SWE-bench images add
    # one environment commit on top of the base (e.g. sphinx's tox.ini gets ``pytest -rA``).
    start_ref: str = "base"
    # Backing services started next to the task container, sharing its network namespace so they
    # answer on localhost (as GitHub Actions services do): [{"name", "image", "env", "ports", "command"}]
    services: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "EnvSpec":
        data = dict(data or {})
        known = {f.name for f in dataclasses.fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"unknown env field(s): {', '.join(unknown)}")
        for key in ("setup", "pre_test", "test_files", "hidden_paths"):
            if isinstance(data.get(key), str):
                data[key] = [data[key]] if data[key] else []
        if data.get("start_ref", "base") not in ("base", "image-head"):
            raise ValueError("start_ref must be 'base' or 'image-head'")
        if data.get("test_file_format", "path") not in TEST_FILE_FORMATS:
            raise ValueError(f"test_file_format must be one of {TEST_FILE_FORMATS}")
        return cls(**data)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass
class Task:
    instance_id: str
    repo: str
    base_commit: str
    problem_statement: str = ""
    requirements: str = ""
    interface: str = ""
    hints: str = ""
    gold_patch: str = ""
    test_patch: str = ""
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    language: str = ""
    source: str = "custom"
    created_at: str = ""
    repo_url: str | None = None
    env: EnvSpec = field(default_factory=EnvSpec)
    metadata: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------ io
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Task":
        data = dict(data)
        missing = [k for k in ("instance_id", "repo", "base_commit") if not data.get(k)]
        if missing:
            raise ValueError(f"task is missing required field(s): {', '.join(missing)}")
        known = {f.name for f in dataclasses.fields(cls)}
        extra = {k: data.pop(k) for k in list(data) if k not in known}
        env = EnvSpec.from_dict(data.pop("env", None))
        for key in ("fail_to_pass", "pass_to_pass"):
            data[key] = _as_list(data.get(key))
        task = cls(env=env, **data)
        if extra:
            task.metadata.setdefault("extra", {}).update(extra)
        return task

    def to_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        out["env"] = self.env.to_dict()
        return out

    # ------------------------------------------------------------ helpers
    @property
    def clone_url(self) -> str:
        """Where to clone the repository from (URL or local path)."""
        if self.repo_url:
            return self.repo_url
        return f"https://github.com/{self.repo}.git"

    def agent_view(self, include_hints: bool = False) -> dict[str, str]:
        """The only task text an agent is allowed to see."""
        view = {
            "instance_id": self.instance_id,
            "repo": self.repo,
            "problem_statement": self.problem_statement,
            "requirements": self.requirements,
            "interface": self.interface,
        }
        if include_hints:
            view["hints"] = self.hints
        return view

    def test_files(self) -> list[str]:
        """Test files to run: explicit ``env.test_files``, else files in the test patch."""
        if self.env.test_files:
            return list(self.env.test_files)
        from .languages import language_of  # local imports: these modules do not need Task
        from .patches import parse_patch

        # fixtures (json, txt, images, ...) live in the test patch too but are not test files
        return [f.path for f in parse_patch(self.test_patch) if not f.deleted and language_of(f.path)]


def _as_list(value: Any) -> list[str]:
    """SWE-bench stores test lists as JSON strings; accept lists, strings or None."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            import ast

            try:
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                return [value]
        value = parsed
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return [str(value)]


# ---------------------------------------------------------------- JSON lines
def read_jsonl(path: str | Path) -> Iterator[dict[str, Any]]:
    """Yield objects from a .jsonl file, or from a .json file holding one object or a list."""
    path = Path(path)
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text) if text.strip() else []
        yield from (data if isinstance(data, list) else [data])
        return
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON ({exc.msg})") from None


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
            count += 1
    return count


def append_jsonl(path: str | Path, row: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def load_tasks(path: str | Path) -> list[Task]:
    """Load tasks in swe-env-kit format (use ``formats.load_any`` for other formats)."""
    return [Task.from_dict(row) for row in read_jsonl(path)]


def save_tasks(path: str | Path, tasks: Iterable[Task]) -> int:
    return write_jsonl(path, (t.to_dict() for t in tasks))
