"""Validate a task: SWE-bench's execution filter plus Pro's repeated gold runs.

* ``pre``  = base + test patch           (expected: the new tests fail)
* ``post`` = base + gold + test patch    (expected: everything passes)

Each is run ``runs`` times. A test is PASS or FAIL in a set of runs only if every run
agrees; otherwise it is FLAKY and excluded from FAIL_TO_PASS / PASS_TO_PASS.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .languages import missing_symbols
from .logparsers import is_pass
from .sandbox import Sandbox
from .stats import mentioned
from .task import Task
from .workspace import TestRun, apply_patch, checkout_base, open_sandbox, run_tests

PASS, FAIL, FLAKY, MISSING = "PASS", "FAIL", "FLAKY", "MISSING"


@dataclass
class ValidationResult:
    instance_id: str
    verdict: str = "invalid"          # valid | invalid | error
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fail_to_pass: list[str] = field(default_factory=list)
    pass_to_pass: list[str] = field(default_factory=list)
    pass_to_fail: list[str] = field(default_factory=list)
    fail_to_fail: list[str] = field(default_factory=list)
    flaky: list[str] = field(default_factory=list)
    naming_errors: list[dict[str, str]] = field(default_factory=list)
    runs: int = 0
    pre_counts: dict[str, int] = field(default_factory=dict)
    post_counts: dict[str, int] = field(default_factory=dict)
    duration: float = 0.0
    log_tails: dict[str, str] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return self.verdict == "valid"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def combine(runs: list[TestRun]) -> dict[str, str]:
    """Merge several runs into PASS / FAIL / FLAKY per test (missing in a run counts as FAIL)."""
    names = set().union(*(r.statuses for r in runs)) if runs else set()
    out = {}
    for n in names:
        seen = {is_pass(r.statuses.get(n)) for r in runs}
        out[n] = FLAKY if len(seen) > 1 else (PASS if seen == {True} else FAIL)
    return out


def classify(pre: dict[str, str], post: dict[str, str]) -> dict[str, list[str]]:
    groups: dict[str, list[str]] = {"f2p": [], "p2p": [], "p2f": [], "f2f": [], "flaky": []}
    for name in sorted(set(pre) | set(post)):
        a, b = pre.get(name, FAIL), post.get(name, MISSING)
        if FLAKY in (a, b):
            groups["flaky"].append(name)
        elif b == MISSING:
            continue  # only seen before the fix (e.g. a test the gold patch removes)
        elif a == FAIL and b == PASS:
            groups["f2p"].append(name)
        elif a == PASS and b == PASS:
            groups["p2p"].append(name)
        elif a == PASS and b == FAIL:
            groups["p2f"].append(name)
        else:
            groups["f2f"].append(name)
    return groups


def _counts(combined: dict[str, str]) -> dict[str, int]:
    c = {PASS: 0, FAIL: 0, FLAKY: 0}
    for v in combined.values():
        c[v] = c.get(v, 0) + 1
    return c


def naming_errors(task: Task, pre_logs: list[str]) -> list[dict[str, str]]:
    """Missing-symbol errors before the fix whose symbol the agent-visible text never names."""
    visible = "\n".join([task.problem_statement, task.requirements, task.interface])
    out, seen = [], set()
    for log in pre_logs:
        for kind, sym in missing_symbols(log):
            leaf = re.split(r"[.:\\/]+", sym)[-1] or sym
            if (kind, leaf) in seen:
                continue
            seen.add((kind, leaf))
            out.append({"kind": kind, "symbol": sym, "specified": mentioned(leaf, visible)})
    return out


def validate_in(sb: Sandbox, task: Task, runs: int = 2, timeout: float | None = None,
                naming: str = "warn") -> ValidationResult:
    """Run the pre/post checks in ``sb``. ``naming="invalid"`` applies SWE-bench's rule and rejects
    tasks whose tests need unspecified new names; the default only warns (Pro's interface fixes it)."""
    res = ValidationResult(task.instance_id, runs=runs)
    pre_runs: list[TestRun] = []
    post_runs: list[TestRun] = []
    for phase, with_gold in (("pre", False), ("post", True)):
        for i in range(runs):
            checkout_base(sb, task)
            if with_gold:
                gold = apply_patch(sb, task.gold_patch, "gold", reset_to=task.base_commit)
                if not gold.ok:
                    res.reasons.append("gold_apply_error")
                    res.log_tails["gold_apply"] = gold.output[-3000:]
                    return res
            tp = apply_patch(sb, task.test_patch, "tests", reset_to=None if with_gold else task.base_commit)
            if not tp.ok:
                res.reasons.append("test_patch_apply_error")
                res.log_tails["test_patch_apply"] = tp.output[-3000:]
                return res
            run = run_tests(sb, task, timeout=timeout, label=f"{phase}{i}")
            res.duration += run.duration
            (post_runs if with_gold else pre_runs).append(run)
            res.log_tails[f"{phase}{i}"] = run.log[-4000:]
            if run.timed_out:
                res.warnings.append(f"{phase}{i}_timed_out")
    pre, post = combine(pre_runs), combine(post_runs)
    res.pre_counts, res.post_counts = _counts(pre), _counts(post)
    groups = classify(pre, post)
    res.fail_to_pass, res.pass_to_pass = groups["f2p"], groups["p2p"]
    res.pass_to_fail, res.fail_to_fail, res.flaky = groups["p2f"], groups["f2f"], groups["flaky"]
    res.naming_errors = naming_errors(task, [r.log for r in pre_runs])
    res.duration = round(res.duration, 2)

    if not any(r.parsed for r in post_runs):
        res.reasons.append("no_tests_parsed")
    elif not res.fail_to_pass:
        res.reasons.append("no_fail_to_pass")
    if res.flaky:
        res.warnings.append(f"flaky_tests:{len(res.flaky)}")
    if res.pass_to_fail:
        res.warnings.append(f"gold_breaks_tests:{len(res.pass_to_fail)}")
    if any(not e["specified"] for e in res.naming_errors):
        (res.reasons if naming == "invalid" else res.warnings).append("naming_leak")
    res.verdict = "invalid" if res.reasons else "valid"
    return res


def validate(task: Task, backend: str = "docker", runs: int = 2, timeout: float | None = None,
             keep: bool = False, naming: str = "warn") -> ValidationResult:
    try:
        with open_sandbox(task, backend, keep=keep) as sb:
            return validate_in(sb, task, runs=runs, timeout=timeout, naming=naming)
    except Exception as exc:
        return ValidationResult(task.instance_id, verdict="error", reasons=[f"{type(exc).__name__}: {exc}"])


def apply_validation(task: Task, result: ValidationResult) -> Task:
    """Write the validated test lists back onto the task."""
    task.fail_to_pass = list(result.fail_to_pass)
    task.pass_to_pass = list(result.pass_to_pass)
    task.metadata["validation"] = {k: getattr(result, k) for k in ("verdict", "reasons", "warnings", "runs")}
    task.metadata["validation"]["flaky"] = list(result.flaky)
    task.metadata["validation"]["naming_errors"] = list(result.naming_errors)
    return task
