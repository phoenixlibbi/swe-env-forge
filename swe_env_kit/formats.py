"""Convert between swe-env-kit tasks and SWE-bench / SWE-bench Pro / generic rows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

from . import presets
from .languages import detect_language
from .patches import files_in
from .task import EnvSpec, Task, _as_list, read_jsonl

FORMATS = ("auto", "swekit", "swebench", "swebench-pro", "generic")


def detect_format(row: dict[str, Any]) -> str:
    if isinstance(row.get("env"), dict) and "gold_patch" in row:
        return "swekit"
    if "requirements" in row and "interface" in row and ("fail_to_pass" in row or "repo_language" in row):
        return "swebench-pro"
    if "patch" in row and "FAIL_TO_PASS" in row:
        return "swebench"
    return "generic"


def from_swebench(row: dict[str, Any]) -> Task:
    iid, repo = row["instance_id"], row["repo"]
    meta = {k: row[k] for k in ("version", "environment_setup_commit", "difficulty") if row.get(k)}
    return Task(
        instance_id=iid, repo=repo, base_commit=row["base_commit"],
        problem_statement=row.get("problem_statement", ""), hints=row.get("hints_text", "") or "",
        gold_patch=row.get("patch", ""), test_patch=row.get("test_patch", ""),
        fail_to_pass=_as_list(row.get("FAIL_TO_PASS")), pass_to_pass=_as_list(row.get("PASS_TO_PASS")),
        language="python", source="swebench", created_at=str(row.get("created_at", "") or ""),
        env=presets.swebench_env(repo, iid, row), metadata=meta,
    )


def from_pro(row: dict[str, Any], scripts: dict[str, str] | None = None) -> Task:
    lang = presets.PRO_LANGUAGES.get(str(row.get("repo_language", "")).lower(), "")
    meta = {k: row[k] for k in ("issue_specificity", "issue_categories", "before_repo_set_cmd",
                                "dockerhub_tag", "version", "hard") if row.get(k) not in (None, "")}
    for k in ("issue_specificity", "issue_categories"):
        if isinstance(meta.get(k), str):
            meta[k] = _as_list(meta[k])
    scripts = scripts or {}
    return Task(
        instance_id=row["instance_id"], repo=row["repo"], base_commit=row["base_commit"],
        problem_statement=row.get("problem_statement", ""), requirements=row.get("requirements", "") or "",
        interface=row.get("interface", "") or "", gold_patch=row.get("patch", ""),
        test_patch=row.get("test_patch", ""), fail_to_pass=_as_list(row.get("fail_to_pass")),
        pass_to_pass=_as_list(row.get("pass_to_pass")), language=lang, source="swebench-pro",
        env=presets.pro_env(row, scripts.get("run_script"), scripts.get("parser_script")), metadata=meta,
    )


# Column aliases for "generic" rows (other SWE-style datasets, internal exports, ...).
_ALIASES = {
    "gold_patch": ("gold_patch", "patch", "golden_patch", "solution_patch", "fix_patch"),
    "test_patch": ("test_patch", "tests_patch"),
    "fail_to_pass": ("fail_to_pass", "FAIL_TO_PASS", "f2p"),
    "pass_to_pass": ("pass_to_pass", "PASS_TO_PASS", "p2p"),
    "problem_statement": ("problem_statement", "issue", "issue_text", "description", "prompt"),
    "hints": ("hints", "hints_text"),
    "created_at": ("created_at", "merged_time", "merged_at"),
    "language": ("language", "repo_language", "lang"),
}
_LANG_NAMES = {"c#": "csharp", "c++": "cpp", "js": "javascript", "ts": "typescript", "py": "python",
               "golang": "go", "rb": "ruby"}


def _first(row: dict, keys: Iterable[str], default: Any = "") -> Any:
    for k in keys:
        if row.get(k) not in (None, ""):
            return row[k]
    return default


def from_generic(row: dict[str, Any]) -> Task:
    """Best-effort mapping for SWE-style rows with their own column names."""
    iid = _first(row, ("instance_id", "task_id", "id"))
    repo = _first(row, ("repo", "repository", "repo_name"))
    base = _first(row, ("base_commit", "base_sha", "base"))
    if not (iid and repo and base):
        raise ValueError("generic row needs instance_id/task_id, repo and base_commit")
    gold = _first(row, _ALIASES["gold_patch"])
    tests = _first(row, _ALIASES["test_patch"])
    lang = str(_first(row, _ALIASES["language"])).strip().lower()
    lang = _LANG_NAMES.get(lang, lang) or detect_language(files_in(gold) + files_in(tests))
    env = EnvSpec(
        image=_first(row, ("image", "docker_image", "image_name"), None),
        dockerfile=_first(row, ("dockerfile",), None),
        workdir=_first(row, ("workdir",), "/testbed"),
        test_command=_first(row, ("test_command", "test_cmd", "eval_command"), None),
        log_parser=_first(row, ("log_parser",), "auto"),
        test_file_format="none" if _first(row, ("test_command", "test_cmd"), None) else "path",
    )
    used = {"instance_id", "task_id", "id", "repo", "repository", "repo_name", "base_commit", "base_sha", "base",
            "image", "docker_image", "image_name", "dockerfile", "workdir", "test_command", "test_cmd",
            "eval_command", "log_parser", *[k for ks in _ALIASES.values() for k in ks]}
    meta = {k: v for k, v in row.items() if k not in used}
    return Task(
        instance_id=str(iid), repo=str(repo), base_commit=str(base),
        problem_statement=str(_first(row, _ALIASES["problem_statement"])),
        requirements=str(row.get("requirements", "") or ""), interface=str(row.get("interface", "") or ""),
        hints=str(_first(row, _ALIASES["hints"])), gold_patch=gold, test_patch=tests,
        fail_to_pass=_as_list(_first(row, _ALIASES["fail_to_pass"], None)),
        pass_to_pass=_as_list(_first(row, _ALIASES["pass_to_pass"], None)),
        language=lang, source="custom", created_at=str(_first(row, _ALIASES["created_at"])),
        env=env, metadata=meta,
    )


def to_task(row: dict[str, Any], fmt: str = "auto") -> Task:
    fmt = detect_format(row) if fmt == "auto" else fmt
    if fmt == "swekit":
        return Task.from_dict(row)
    if fmt == "swebench":
        return from_swebench(row)
    if fmt == "swebench-pro":
        return from_pro(row)
    if fmt == "generic":
        return from_generic(row)
    raise ValueError(f"unknown format {fmt!r}; choose from {', '.join(FORMATS)}")


def load_any(path: str | Path, fmt: str = "auto") -> list[Task]:
    """Load tasks from a .jsonl/.json file in any supported format."""
    return [to_task(row, fmt) for row in read_jsonl(path)]


# ---------------------------------------------------------------------- export
def to_swebench(task: Task) -> dict[str, Any]:
    return {
        "instance_id": task.instance_id, "repo": task.repo, "base_commit": task.base_commit,
        "problem_statement": task.problem_statement, "hints_text": task.hints,
        "patch": task.gold_patch, "test_patch": task.test_patch,
        "FAIL_TO_PASS": json.dumps(task.fail_to_pass), "PASS_TO_PASS": json.dumps(task.pass_to_pass),
        "created_at": task.created_at, "version": task.metadata.get("version", ""),
        "environment_setup_commit": task.metadata.get("environment_setup_commit", task.base_commit),
    }


def to_pro(task: Task) -> dict[str, Any]:
    lang = {v: k for k, v in presets.PRO_LANGUAGES.items() if len(k) <= 2}.get(task.language, task.language)
    return {
        "repo": task.repo, "instance_id": task.instance_id, "base_commit": task.base_commit,
        "patch": task.gold_patch, "test_patch": task.test_patch, "problem_statement": task.problem_statement,
        "requirements": task.requirements, "interface": task.interface, "repo_language": lang,
        "fail_to_pass": json.dumps(task.fail_to_pass), "pass_to_pass": json.dumps(task.pass_to_pass),
        "selected_test_files_to_run": json.dumps(task.test_files()),
        "docker_image": task.env.image or "",
    }


EXPORTERS = {"swekit": Task.to_dict, "swebench": to_swebench, "swebench-pro": to_pro}


# ----------------------------------------------------------------- predictions
def load_predictions(path: str | Path) -> dict[str, str]:
    """Agent patches keyed by instance_id.

    Accepts SWE-bench predictions (``model_patch``), Pro patch files (``patch``), our
    run summaries (``patch``) and ``{instance_id: patch}`` JSON maps.
    """
    path = Path(path)
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        if isinstance(data, dict) and all(isinstance(v, str) for v in data.values()):
            return dict(data)
        if isinstance(data, dict):
            rows = [{"instance_id": k, **v} if isinstance(v, dict) else {"instance_id": k, "patch": v}
                    for k, v in data.items()]
        else:
            rows = data
    else:
        rows = list(read_jsonl(path))
    out = {}
    for r in rows:
        patch = r.get("model_patch", r.get("patch"))
        if r.get("instance_id") and patch is not None:
            out[r["instance_id"]] = patch or ""
    return out
