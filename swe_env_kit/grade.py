"""Grade a candidate patch: tamper guard, hidden tests, binary and dense reward."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .languages import adds_skip_marker
from .logparsers import is_pass
from .patches import parse_patch
from .sandbox import Sandbox
from .task import Task
from .workspace import apply_patch, checkout_base, open_sandbox, revert_files, run_tests


@dataclass
class GradeResult:
    instance_id: str
    resolved: bool = False
    dense: float = 0.0
    f2p_rate: float = 0.0
    p2p_rate: float = 0.0
    f2p_passed: list[str] = field(default_factory=list)
    f2p_failed: list[str] = field(default_factory=list)
    p2p_failed: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    patch_applied: bool = False
    apply_method: str = ""
    failure: str = ""            # empty when resolved
    flags: list[str] = field(default_factory=list)
    reverted_test_files: list[str] = field(default_factory=list)
    harness_error: str = ""
    duration: float = 0.0
    test_log_tail: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def tamper_flags(patch: str, test_patch_files: set[str]) -> tuple[list[str], list[str]]:
    """(flags, files to revert) for a candidate patch.

    Files the hidden test patch touches are always reverted. Other edits to test files,
    deleted test files and new skip markers are flagged.
    """
    flags, revert = [], []
    for f in parse_patch(patch):
        if f.path in test_patch_files or f.old_path in test_patch_files:
            revert.append(f.path)
            flags.append(f"edits_hidden_test_file:{f.path}")
            continue
        if f.is_test:
            flags.append(f"{'deletes' if f.deleted else 'edits'}_test_file:{f.path}")
        if any(adds_skip_marker(ln) for ln in f.added):
            flags.append(f"adds_skip_marker:{f.path}")
        if f.path.rsplit("/", 1)[-1] in ("conftest.py", "pytest.ini", "tox.ini", "setup.cfg", "jest.config.js",
                                          "jest.config.ts", "vitest.config.ts", ".mocharc.json", "phpunit.xml"):
            flags.append(f"edits_test_config:{f.path}")
    return flags, revert


def score(task: Task, statuses: dict[str, str]) -> dict[str, Any]:
    f2p, p2p = task.fail_to_pass, task.pass_to_pass
    f2p_ok = [t for t in f2p if is_pass(statuses.get(t))]
    p2p_bad = [t for t in p2p if not is_pass(statuses.get(t))]
    missing = [t for t in [*f2p, *p2p] if t not in statuses]
    f2p_rate = len(f2p_ok) / len(f2p) if f2p else 1.0
    p2p_rate = (len(p2p) - len(p2p_bad)) / len(p2p) if p2p else 1.0
    return {
        "resolved": bool(f2p) and len(f2p_ok) == len(f2p) and not p2p_bad,
        "f2p_rate": round(f2p_rate, 4), "p2p_rate": round(p2p_rate, 4),
        "dense": round(f2p_rate * p2p_rate, 4),
        "f2p_passed": f2p_ok, "f2p_failed": [t for t in f2p if t not in f2p_ok], "p2p_failed": p2p_bad,
        "missing": missing,
    }


def grade_in(sb: Sandbox, task: Task, patch: str, *, strict: bool = False, timeout: float | None = None) -> GradeResult:
    """Grade ``patch`` in an already-open sandbox (the tree is reset to the base commit first)."""
    r = GradeResult(task.instance_id)
    if not task.fail_to_pass:
        r.harness_error = "task has no FAIL_TO_PASS tests (run `swekit validate` first)"
        r.failure = "harness_error"
        return r
    checkout_base(sb, task)
    test_files = {f.path for f in parse_patch(task.test_patch)}
    flags, revert = tamper_flags(patch, test_files)
    r.flags = flags

    if not patch.strip():
        r.failure = "empty_patch"
    applied = apply_patch(sb, patch, "candidate", reset_to=task.base_commit)
    r.patch_applied, r.apply_method = applied.ok, applied.method
    if applied.skipped_binary:
        r.flags.append("binary_files_skipped:" + ",".join(applied.skipped_binary))
    if not applied.ok:
        r.failure = "patch_apply_error"
        r.test_log_tail = applied.output[-4000:]
        return r
    if revert:
        revert_files(sb, task.base_commit, revert)
        r.reverted_test_files = revert

    tests = apply_patch(sb, task.test_patch, "hidden_tests")
    if not tests.ok:
        r.harness_error = "hidden test patch did not apply"
        r.failure = "harness_error"
        r.test_log_tail = tests.output[-4000:]
        return r

    run = run_tests(sb, task, timeout=timeout, label="grade")
    r.duration = round(run.duration, 2)
    r.test_log_tail = run.log[-6000:]
    if run.timed_out:
        r.flags.append("tests_timed_out")
    if not run.parsed:
        r.failure = "tests_not_run"
        return r
    s = score(task, run.statuses)
    for k, v in s.items():
        setattr(r, k, v)
    if strict and any(f.split(":")[0] in ("adds_skip_marker", "edits_test_config", "deletes_test_file")
                      for f in r.flags):
        r.resolved, r.dense = False, 0.0
        r.failure = "tampering"
    if not r.resolved and not r.failure:
        if not r.f2p_failed and r.p2p_failed:
            r.failure = "regression"
        elif r.f2p_passed:
            r.failure = "partial"
        else:
            r.failure = "wrong_solution"
    if r.resolved:
        r.failure = ""
    return r


def grade(task: Task, patch: str, backend: str = "docker", *, strict: bool = False,
          timeout: float | None = None, keep: bool = False) -> GradeResult:
    """Grade ``patch`` in a fresh sandbox."""
    try:
        with open_sandbox(task, backend, keep=keep) as sb:
            return grade_in(sb, task, patch, strict=strict, timeout=timeout)
    except Exception as exc:  # environment problems must not look like agent failures
        r = GradeResult(task.instance_id, failure="harness_error", harness_error=f"{type(exc).__name__}: {exc}")
        return r
