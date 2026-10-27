"""Validate a task: SWE-bench's execution filter plus Pro's repeated gold runs.

* ``pre``  = base + test patch           (expected: the new tests fail)
* ``post`` = base + gold + test patch    (expected: everything passes)

Each is run ``runs`` times. A test is PASS or FAIL in a set of runs only if every run
agrees; otherwise it is FLAKY and excluded from FAIL_TO_PASS / PASS_TO_PASS.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import asdict, dataclass, field
from typing import Any

from .languages import missing_symbols
from .logparsers import is_pass
from .sandbox import Sandbox
from .stats import mentioned
from .task import Task
from .workspace import TestRun, apply_patch, checkout_base, open_sandbox, run_tests, start_ref

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
    masked_f2p: list[str] = field(default_factory=list)   # "F2P" tests that already passed before the change
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
    """Merge several runs into PASS / FAIL / FLAKY per test (missing in a run counts as FAIL).

    Runs that produced no results at all (a crash or timeout) are ignored when another run
    did produce results, so one crash does not turn every test into FLAKY.
    """
    parsed = [r for r in runs if r.parsed]
    runs = parsed or runs
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
            if a == PASS:  # passed before the fix, gone after it: the gold patch broke it
                groups["p2f"].append(name)
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


_BUILTIN_OWNERS = re.compile(r"'(?:NoneType|list|dict|str|int|float|tuple|set|bool|bytes)' object has no attribute")


def symbol_leaf(sym: str) -> str:
    """The name a reader would recognise: ``pkg.mod.func`` -> ``func``, ``./lib/helper.js`` -> ``helper``."""
    if "/" in sym or "\\" in sym:
        base = re.split(r"[/\\]+", sym.rstrip("/\\"))[-1]
        return re.sub(r"\.[A-Za-z0-9]+$", "", base) or base
    return re.split(r"[.:]+", sym)[-1] or sym


def naming_errors(task: Task, pre_logs: list[str], post_logs: list[str] | None = None) -> list[dict[str, str]]:
    """Missing-symbol errors that appear before the fix but not after it, i.e. names the fix creates.

    Each is marked ``specified`` when the agent-visible text (statement, requirements,
    interface) mentions it. AttributeErrors on built-in values (``'NoneType' object has no
    attribute ...``) are symptoms of the bug, not missing names, and are ignored.
    """
    visible = "\n".join([task.problem_statement, task.requirements, task.interface])
    after = {(k, s) for log in (post_logs or []) for k, s in missing_symbols(log)}
    out, seen = [], set()
    for log in pre_logs:
        builtin_attr = {m.group(0) for m in re.finditer(_BUILTIN_OWNERS.pattern + r" '(\w+)'", log)}
        for kind, sym in missing_symbols(log):
            if (kind, sym) in after:
                continue
            if kind == "python-attribute" and any(f"'{sym}'" in b for b in builtin_attr):
                continue
            leaf = symbol_leaf(sym)
            if (kind, leaf) in seen:
                continue
            seen.add((kind, leaf))
            out.append({"kind": kind, "symbol": sym, "specified": mentioned(leaf, visible)})
    return out


def _base_run(sb: Sandbox, task: Task, timeout: float | None) -> TestRun | None:
    """The tests at the start commit *without* the test patch (only the test files that exist there)."""
    checkout_base(sb, task)
    ref = start_ref(sb, task)
    files = [f for f in task.test_files()
             if sb.exec(f"git -c 'safe.directory=*' cat-file -e {shlex.quote(ref)}:{shlex.quote(f)}", timeout=60).ok]
    if task.env.test_file_format != "none" and not files:
        return None
    return run_tests(sb, task, timeout=timeout, label="base", files=files)


_TEST_DEF = re.compile(r"^(\s*)(?:async\s+)?def\s+(\w+)\s*\(|^(\s*)class\s+(\w+)")


def changed_test_functions(sb: Sandbox, task: Task) -> dict[str, set[str]] | None:
    """{test file: names of test functions whose body the test patch adds or changes}.

    Read from the patched files in ``sb`` (call it after the test patch is applied). Only Python
    files are analysed; returns ``None`` when any test file is in another language, so callers can
    fall back to the conservative behaviour.
    """
    from .languages import language_of
    from .patches import parse_patch

    out: dict[str, set[str]] = {}
    for f in parse_patch(task.test_patch):
        if f.deleted or not language_of(f.path):
            continue
        if language_of(f.path) != "python":
            return None
        try:
            lines = sb.read_file(f.path).split("\n")
        except Exception:
            return None
        changed: set[int] = set()
        for h in f.hunks:
            n = h.new_start
            for ln in h.lines:
                if ln.startswith("+"):
                    changed.add(n)
                    n += 1
                elif ln.startswith("-"):
                    changed.add(n)       # a deletion changes the function around this point
                else:
                    n += 1
        names: set[str] = set()
        for lineno in changed:
            if not 1 <= lineno <= len(lines):
                continue
            line = lines[lineno - 1]
            if not line.strip():
                continue  # blank lines belong to nothing
            m = _TEST_DEF.match(line)
            if m and m.group(2):
                names.add(m.group(2))  # the definition line itself
                continue
            indent = len(line) - len(line.lstrip())
            # walk up to the nearest function that encloses this line (strictly smaller indentation)
            for i in range(lineno - 2, -1, -1):
                m = _TEST_DEF.match(lines[i])
                if m and m.group(2) and len(m.group(1)) < indent:
                    names.add(m.group(2))
                    break
                if lines[i].strip() and len(lines[i]) - len(lines[i].lstrip()) == 0 and indent > 0 and not m:
                    break  # left the top-level block without finding a function
        out[f.path] = names
    return out


def _test_name_matches(test_id: str, names: set[str]) -> bool:
    leaf = re.split(r"::|\.", test_id.split("[", 1)[0])[-1]
    return leaf in names


_ENV_MISSING_KINDS = ("python-module", "js-module", "ruby-load", "c-missing-header", "ts-TS2307")


def validate_in(sb: Sandbox, task: Task, runs: int = 2, timeout: float | None = None,
                naming: str = "warn", base_run: bool = True) -> ValidationResult:
    """Run the pre/post checks in ``sb``. ``naming="invalid"`` applies SWE-bench's rule and rejects
    tasks whose tests need unspecified new names; the default only warns (Pro's interface fixes it)."""
    res = ValidationResult(task.instance_id, runs=runs)
    pre_runs: list[TestRun] = []
    post_runs: list[TestRun] = []
    changed_fns: dict[str, set[str]] | None = None
    for phase, with_gold in (("pre", False), ("post", True)):
        for i in range(runs):
            checkout_base(sb, task)
            if with_gold:
                gold = apply_patch(sb, task.gold_patch, "gold", reset_to=start_ref(sb, task))
                if not gold.ok:
                    res.reasons.append("gold_apply_error")
                    res.log_tails["gold_apply"] = gold.output[-3000:]
                    return res
            tp = apply_patch(sb, task.test_patch, "tests", reset_to=None if with_gold else start_ref(sb, task))
            if not tp.ok:
                res.reasons.append("test_patch_apply_error")
                res.log_tails["test_patch_apply"] = tp.output[-3000:]
                return res
            if with_gold and i == 0 and base_run:
                changed_fns = changed_test_functions(sb, task)
            run = run_tests(sb, task, timeout=timeout, label=f"{phase}{i}")
            res.duration += run.duration
            (post_runs if with_gold else pre_runs).append(run)
            res.log_tails[f"{phase}{i}"] = run.log[-4000:]
            if run.timed_out:
                res.warnings.append(f"{phase}{i}_timed_out")
    pre, post = combine(pre_runs), combine(post_runs)
    res.pre_counts, res.post_counts = _counts(pre), _counts(post)
    groups = classify(pre, post)
    # A test can "fail" before the fix only because the new test patch stops its file from importing
    # (it needs a name the fix creates). If it passed at the start commit with the old test files, it is
    # really pass-to-pass: counting it as F2P would hand out reward for merely defining that name.
    if base_run and groups["f2p"]:
        base = _base_run(sb, task, timeout)
        if base is not None and base.parsed:
            res.log_tails["base"] = base.log[-4000:]
            all_changed = set().union(*changed_fns.values()) if changed_fns else set()
            # a test whose body the test patch changed is a new check even if its old version passed
            res.masked_f2p = [t for t in groups["f2p"] if is_pass(base.statuses.get(t))
                              and not (changed_fns is not None and _test_name_matches(t, all_changed))]
            if res.masked_f2p and len(res.masked_f2p) < len(groups["f2p"]):
                groups["f2p"] = [t for t in groups["f2p"] if t not in res.masked_f2p]
                groups["p2p"] = sorted(groups["p2p"] + res.masked_f2p)
                res.warnings.append(f"f2p_masked_by_import_error:{len(res.masked_f2p)}")
            else:
                if res.masked_f2p:  # every F2P test passed before the change: suspicious, keep and warn
                    res.warnings.append("f2p_all_passed_before_change")
                res.masked_f2p = []
    res.fail_to_pass, res.pass_to_pass = groups["f2p"], groups["p2p"]
    res.pass_to_fail, res.fail_to_fail, res.flaky = groups["p2f"], groups["f2f"], groups["flaky"]
    res.naming_errors = naming_errors(task, [r.log for r in pre_runs], [r.log for r in post_runs])
    crashed = [i for i, r in enumerate(post_runs) if not r.parsed]
    if crashed and len(crashed) < len(post_runs):
        res.warnings.append(f"post_runs_without_results:{len(crashed)}")
    res.duration = round(res.duration, 2)

    # Names still missing after the fix are not the task's fault: the environment lacks a dependency.
    env_missing = sorted({s for r in post_runs for k, s in missing_symbols(r.log) if k in _ENV_MISSING_KINDS})
    if not any(r.parsed for r in post_runs):
        res.reasons.append(f"environment_incomplete:{','.join(env_missing)}" if env_missing else "no_tests_parsed")
    elif not res.fail_to_pass:
        res.reasons.append(f"environment_incomplete:{','.join(env_missing)}" if env_missing else "no_fail_to_pass")
    elif env_missing:
        res.warnings.append(f"post_missing_modules:{','.join(env_missing)}")
    if res.flaky:
        res.warnings.append(f"flaky_tests:{len(res.flaky)}")
    if res.pass_to_fail:
        res.warnings.append(f"gold_breaks_tests:{len(res.pass_to_fail)}")
    if any(not e["specified"] for e in res.naming_errors):
        (res.reasons if naming == "invalid" else res.warnings).append("naming_leak")
    res.verdict = "invalid" if res.reasons else "valid"
    return res


def validate(task: Task, backend: str = "docker", runs: int = 2, timeout: float | None = None,
             keep: bool = False, naming: str = "warn", base_run: bool = True) -> ValidationResult:
    try:
        with open_sandbox(task, backend, keep=keep) as sb:
            return validate_in(sb, task, runs=runs, timeout=timeout, naming=naming, base_run=base_run)
    except Exception as exc:
        return ValidationResult(task.instance_id, verdict="error", reasons=[f"{type(exc).__name__}: {exc}"])


def apply_validation(task: Task, result: ValidationResult) -> Task:
    """Write the validated test lists back onto the task."""
    task.fail_to_pass = list(result.fail_to_pass)
    task.pass_to_pass = list(result.pass_to_pass)
    task.metadata["validation"] = {k: getattr(result, k) for k in ("verdict", "reasons", "warnings", "runs")}
    task.metadata["validation"]["flaky"] = list(result.flaky)
    task.metadata["validation"]["masked_f2p"] = list(result.masked_f2p)
    task.metadata["validation"]["naming_errors"] = list(result.naming_errors)
    return task
