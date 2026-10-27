"""Per-task statistics: fix size, triviality, Pro-eligibility, static naming leaks, license class."""

from __future__ import annotations

import re
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable

from .languages import detect_language
from .patches import added_definitions, added_lines, files_in, identifiers, patch_stats
from .task import Task

PERMISSIVE = ("MIT", "APACHE", "BSD", "ISC", "UNLICENSE", "0BSD", "ZLIB", "PSF", "PYTHON", "CC0", "BSL-1.0", "WTFPL", "X11")
COPYLEFT = ("AGPL", "LGPL", "GPL", "MPL", "EPL", "EUPL", "CDDL", "OSL", "CECILL")


def license_class(spdx: str | None) -> str:
    """'copyleft', 'permissive' or 'unknown' for an SPDX id such as 'GPL-3.0' or 'MIT'."""
    if not spdx or spdx.upper() in ("NOASSERTION", "OTHER", "NONE"):
        return "unknown"
    up = spdx.upper()
    if any(up.startswith(c) or f"-{c}" in up for c in COPYLEFT):
        return "copyleft"
    if any(up.startswith(p) for p in PERMISSIVE):
        return "permissive"
    return "unknown"


def mentioned(name: str, text: str) -> bool:
    return re.search(rf"(?<![\w$]){re.escape(name)}(?![\w$])", text) is not None


@dataclass
class TaskStats:
    instance_id: str
    repo: str
    language: str
    gold: dict[str, int]
    tests: dict[str, int]
    n_fail_to_pass: int
    n_pass_to_pass: int
    trivial: bool                 # 1-2 edited lines
    pro_eligible: bool            # >= min_lines changed (Pro excludes 1-10 line edits)
    new_symbols_used_by_tests: list[str] = field(default_factory=list)
    unspecified_symbols: list[str] = field(default_factory=list)
    license: str = ""
    license_class: str = "unknown"

    @property
    def naming_leak(self) -> bool:
        return bool(self.unspecified_symbols)

    def to_dict(self) -> dict[str, Any]:
        d = dict(self.__dict__)
        d["naming_leak"] = self.naming_leak
        return d


def task_stats(task: Task, min_lines: int = 10, license_spdx: str | None = None) -> TaskStats:
    gold = patch_stats(task.gold_patch)
    tests = patch_stats(task.test_patch)
    language = task.language or detect_language(files_in(task.gold_patch) + files_in(task.test_patch))

    # Static naming-leak check: names the gold patch *creates* that the hidden tests use,
    # but that the agent-visible text never mentions.
    test_idents = identifiers(added_lines(task.test_patch))
    new_defs = [d for d in added_definitions(task.gold_patch) if d.is_new]
    used = sorted({d.name for d in new_defs if d.name in test_idents and len(d.name) > 2})
    visible = "\n".join([task.problem_statement, task.requirements, task.interface])
    unspecified = [n for n in used if not mentioned(n, visible)]
    spdx = license_spdx or task.metadata.get("license") or ""
    return TaskStats(
        instance_id=task.instance_id, repo=task.repo, language=language,
        gold=gold.to_dict(), tests=tests.to_dict(),
        n_fail_to_pass=len(task.fail_to_pass), n_pass_to_pass=len(task.pass_to_pass),
        trivial=0 < gold.lines_edited <= 2, pro_eligible=gold.lines_changed >= min_lines,
        new_symbols_used_by_tests=used, unspecified_symbols=unspecified,
        license=spdx, license_class=license_class(spdx),
    )


def _dist(values: list[float]) -> dict[str, float]:
    if not values:
        return {"mean": 0.0, "median": 0.0, "max": 0.0}
    return {"mean": round(statistics.fmean(values), 2), "median": float(statistics.median(values)),
            "max": float(max(values))}


def summarize(stats: Iterable[TaskStats]) -> dict[str, Any]:
    stats = list(stats)
    n = len(stats)
    pct = (lambda k: round(100.0 * k / n, 1)) if n else (lambda k: 0.0)
    trivial = sum(s.trivial for s in stats)
    eligible = sum(s.pro_eligible for s in stats)
    leaks = sum(s.naming_leak for s in stats)
    uses_new = sum(bool(s.new_symbols_used_by_tests) for s in stats)
    repos = Counter(s.repo for s in stats)
    return {
        "tasks": n,
        "repos": len(repos),
        "largest_repo_share_pct": pct(repos.most_common(1)[0][1]) if repos else 0.0,
        "languages": dict(Counter(s.language or "unknown" for s in stats).most_common()),
        "license_classes": dict(Counter(s.license_class for s in stats).most_common()),
        "trivial_1_2_lines": {"count": trivial, "pct": pct(trivial)},
        "pro_eligible": {"count": eligible, "pct": pct(eligible)},
        "tests_use_new_symbols": {"count": uses_new, "pct": pct(uses_new)},
        "naming_leak_unspecified": {"count": leaks, "pct": pct(leaks)},
        "gold_lines_changed": _dist([s.gold["lines_changed"] for s in stats]),
        "gold_lines_edited": _dist([s.gold["lines_edited"] for s in stats]),
        "gold_files": _dist([s.gold["files"] for s in stats]),
        "fail_to_pass": _dist([s.n_fail_to_pass for s in stats]),
        "pass_to_pass": _dist([s.n_pass_to_pass for s in stats]),
    }
