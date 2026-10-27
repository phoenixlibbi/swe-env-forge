"""Grade a candidate patch: tamper guard, hidden tests, binary and dense reward."""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any

from .languages import adds_skip_marker, is_test_file_name
from .logparsers import is_pass
from .patches import parse_patch
from .sandbox import Sandbox
from .task import Task
from .workspace import apply_patch, checkout_base, open_sandbox, revert_files, run_tests

_q = shlex.quote


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


# Files that only configure test runs: the agent's edits to them are always reverted.
TEST_INFRA_FILES = {"conftest.py", "pytest.ini", ".mocharc.json", ".mocharc.js", ".mocharc.cjs", ".mocharc.yml",
                    ".mocharc.yaml", "karma.conf.js", "phpunit.xml", "phpunit.xml.dist", ".rspec", "spec_helper.rb",
                    "rails_helper.rb", "test_helper.rb", "jest.setup.js", "jest.setup.ts", "setupTests.js",
                    "setupTests.ts", "xunit.runner.json", ".runsettings"}
TEST_INFRA_RE = re.compile(r"^(jest|vitest|vite|playwright|cypress|ava|karma)\.config\.[cm]?[jt]s$")
# Shared build files may legitimately change; they are flagged when the changed lines touch test settings.
MIXED_CONFIG_FILES = {"pyproject.toml", "setup.cfg", "tox.ini", "package.json", "Cargo.toml", "go.mod",
                      "pom.xml", "build.gradle", "build.gradle.kts", "Makefile", "noxfile.py", ".bazelrc"}
TEST_SETTING_RE = re.compile(r"pytest|addopts|testpaths|python_files|conftest|-p\s|\bplugins?\b|jest|vitest|mocha"
                             r"|\"test\"\s*:|surefire|skipTests|testOptions|\[\[test\]\]|\[profile\.test|harness",
                             re.I)
STRICT_FLAGS = ("adds_skip_marker", "edits_test_config")


def _name(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def tamper_flags(patch: str, test_patch_files: set[str], source_files: set[str] = frozenset()) -> tuple[list[str], list[str]]:
    """(flags, paths to revert) for a candidate patch.

    Reverted: every test file the candidate touches (hidden or not), files under test
    directories (helpers the tests import) unless ``source_files`` (the files the reference
    fix edits) says they are real source, and pure test-runner config (conftest.py,
    jest.config.js, ...). Flagged: skip markers added to other files and test-related edits
    to shared build files (pyproject.toml, package.json, ...).
    """
    flags, revert = [], []
    for f in parse_patch(patch):
        paths = {f.path, f.old_path} - {""}
        hidden = bool(paths & test_patch_files)
        infra = any(_name(p) in TEST_INFRA_FILES or TEST_INFRA_RE.match(_name(p)) for p in paths)
        test_file = any(is_test_file_name(p) for p in paths)
        test_dir = f.is_test and not (paths & set(source_files))
        if hidden or test_file or infra or test_dir:
            revert.extend(sorted(paths))
            kind = ("edits_hidden_test_file" if hidden else "edits_test_config" if infra
                    else "deletes_test_file" if f.deleted else "edits_test_file")
            flags.append(f"{kind}:{f.path}")
            continue
        if any(adds_skip_marker(ln) for ln in f.added):
            flags.append(f"adds_skip_marker:{f.path}")
        if _name(f.path) in MIXED_CONFIG_FILES and any(TEST_SETTING_RE.search(ln) for ln in f.added + f.removed):
            flags.append(f"edits_test_config:{f.path}")
    return flags, revert


def _clear_parent_files(sb: Sandbox, paths: list[str]) -> None:
    """Remove files or symlinks the candidate put where the test patch needs a directory."""
    parents = sorted({"/".join(p.split("/")[:i]) for p in paths for i in range(1, p.count("/") + 1)})
    if parents:
        checks = "; ".join(f"if [ -e {_q(d)} ] && [ ! -d {_q(d)} ] || [ -L {_q(d)} ]; then rm -f -- {_q(d)}; fi"
                           for d in parents)
        sb.exec(checks, timeout=120)


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


def grade_in(sb: Sandbox, task: Task, patch: str, *, strict: bool = True, timeout: float | None = None) -> GradeResult:
    """Grade ``patch`` in an already-open sandbox (the tree is reset to the base commit first).

    ``strict`` (the default) fails candidates that add skip markers or change test settings
    in shared build files. Edits to test files and test-runner config are always reverted.
    """
    r = GradeResult(task.instance_id)
    if not task.fail_to_pass:
        r.harness_error = "task has no FAIL_TO_PASS tests (run `swekit validate` first)"
        r.failure = "harness_error"
        return r
    checkout_base(sb, task)
    tp_files = parse_patch(task.test_patch)
    test_paths = sorted({p for f in tp_files for p in (f.path, f.old_path) if p})
    flags, revert = tamper_flags(patch, set(test_paths), {p for f in parse_patch(task.gold_patch)
                                                        for p in (f.path, f.old_path) if p})
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
    # Always restore every path the hidden tests touch, plus anything flagged above, then make
    # sure no file or symlink sits where the test patch needs a directory.
    failed = revert_files(sb, task.base_commit, [*test_paths, *revert])
    r.reverted_test_files = sorted(set(revert))
    _clear_parent_files(sb, test_paths)

    tests = apply_patch(sb, task.test_patch, "hidden_tests")
    if tests.skipped_binary:
        r.flags.append("hidden_tests_binary_skipped:" + ",".join(tests.skipped_binary))
    if not tests.ok:
        r.test_log_tail = tests.output[-4000:]
        # Did the candidate cause this? Check that the hidden tests apply to a clean base.
        checkout_base(sb, task)
        if apply_patch(sb, task.test_patch, "hidden_tests_clean").ok:
            r.failure = "tampering"
            r.flags.append("blocks_hidden_tests" + (f":{','.join(failed)}" if failed else ""))
        else:
            r.harness_error = "hidden test patch does not apply to the base commit"
            r.failure = "harness_error"
        return r

    run = run_tests(sb, task, timeout=timeout, label="grade")
    r.duration = round(run.duration, 2)
    r.test_log_tail = run.log[-6000:]
    if run.timed_out:
        r.flags.append("tests_timed_out")
    if not run.parsed:
        r.failure = r.failure or "tests_not_run"  # e.g. compile errors; keep "empty_patch" when it applies
        return r
    s = score(task, run.statuses)
    for k, v in s.items():
        setattr(r, k, v)
    if strict and any(f.split(":")[0] in STRICT_FLAGS for f in r.flags):
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


def grade(task: Task, patch: str, backend: str = "docker", *, strict: bool = True,
          timeout: float | None = None, keep: bool = False) -> GradeResult:
    """Grade ``patch`` in a fresh sandbox."""
    sb = None
    try:
        sb = open_sandbox(task, backend, keep=keep)
        return grade_in(sb, task, patch, strict=strict, timeout=timeout)
    except Exception as exc:  # environment problems must not look like agent failures
        return GradeResult(task.instance_id, failure="harness_error", harness_error=f"{type(exc).__name__}: {exc}")
    finally:
        if sb is not None:
            try:
                sb.close()
            except Exception:  # a slow `docker rm` must not turn a finished grade into an error
                pass
