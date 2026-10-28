"""Aggregate runs: resolve rate and dense reward by slice, budget curves, failure modes.

Budget curves answer the "which number is the real score?" question from the Pro
paper (about 23% under a $2 cap vs 43.6% uncapped): from ONE run they give the score
an agent would have had under any turn or cost cap.
"""

from __future__ import annotations

import math
import random
import statistics
from collections import defaultdict
from typing import Any, Iterable

from .failure import distribution
from .patches import patch_stats
from .task import Task
from .trajectory import Episode

LINE_BUCKETS = ((1, 9, "1-9"), (10, 49, "10-49"), (50, 99, "50-99"), (100, 499, "100-499"), (500, 10**9, "500+"))
FILE_BUCKETS = ((1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 9, "4-9"), (10, 10**9, "10+"))
DEFAULT_TURN_CAPS = (5, 10, 20, 30, 50, 100)
DEFAULT_COST_CAPS = (0.25, 0.5, 1.0, 2.0, 5.0)


def _bucket(value: int, buckets) -> str:
    for lo, hi, name in buckets:
        if lo <= value <= hi:
            return name
    return "0"


def _cum_costs(ep: Episode) -> dict[int, float]:
    cum, costs = 0.0, {0: 0.0}
    for s in ep.steps:
        cum += s.cost
        costs[s.turn] = cum
    return costs


def resolved_if_stopped(ep: Episode, turn_cap: float = float("inf"), cost_cap: float = float("inf")) -> bool:
    """Would the episode count as resolved had it been stopped at the cap?

    A stopped run submits its latest patch, so this is the grade of the last checkpoint within the cap
    (the final grade when the whole run fits). An early resolving patch that was later broken does not
    count for caps after it was broken, and never for the uncapped score.
    """
    costs = _cum_costs(ep)
    final_cost = ep.cost or max(costs.values())
    if ep.turns <= turn_cap and final_cost <= cost_cap:
        return bool(ep.grade and ep.grade.get("resolved"))
    within = [cp for cp in ep.checkpoints if cp.turn <= turn_cap and costs.get(cp.turn, final_cost) <= cost_cap]
    if not within:
        return False
    last = max(within, key=lambda c: c.turn)
    return bool(last.grade and last.grade.get("resolved"))


def solved_at(ep: Episode) -> tuple[int, float] | None:
    """(turn, cumulative cost) from which the episode stays resolved to the end, or None."""
    if not (ep.grade and ep.grade.get("resolved")):
        return None
    costs = _cum_costs(ep)
    turn = ep.turns
    for cp in sorted(ep.checkpoints, key=lambda c: c.turn, reverse=True):
        if not (cp.grade and cp.grade.get("resolved")):
            break
        turn = cp.turn
    return turn, costs.get(turn, ep.cost)


def budget_curves(episodes: list[Episode], turn_caps: Iterable[int] = DEFAULT_TURN_CAPS,
                  cost_caps: Iterable[float] = DEFAULT_COST_CAPS) -> dict[str, Any]:
    n = len(episodes)
    pct = lambda k: round(100.0 * k / n, 1) if n else 0.0  # noqa: E731
    return {
        "by_turns": {str(t): pct(sum(resolved_if_stopped(e, turn_cap=t) for e in episodes)) for t in turn_caps},
        "by_cost": {f"{c:g}": pct(sum(resolved_if_stopped(e, cost_cap=c) for e in episodes)) for c in cost_caps},
        "uncapped": pct(sum(bool(e.grade and e.grade.get("resolved")) for e in episodes)),
    }


# ----------------------------------------------------------------- statistics
def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a proportion, in percent."""
    if n == 0:
        return 0.0, 0.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return round(100 * max(0.0, centre - half), 1), round(100 * min(1.0, centre + half), 1)


def pass_at_k(n: int, c: int, k: int) -> float:
    """Unbiased pass@k from n samples with c correct (Chen et al., 2021)."""
    if n - c < k:
        return 1.0
    return 1.0 - math.prod((n - c - i) / (n - i) for i in range(k))


def repo_bootstrap(results: list[dict[str, Any]], tasks: dict[str, Task], iters: int = 1000,
                   seed: int = 0) -> tuple[float, float] | None:
    """95% interval of the resolve rate, resampling whole repositories (deterministic seed)."""
    by_repo: dict[str, list[bool]] = defaultdict(list)
    for r in results:
        t = tasks.get(r["instance_id"])
        by_repo[t.repo if t else "?"].append(bool(r.get("resolved")))
    repos = list(by_repo.values())
    if len(repos) < 2:
        return None
    rng = random.Random(seed)
    rates = []
    for _ in range(iters):
        pick = [rng.choice(repos) for _ in repos]
        total = sum(len(p) for p in pick)
        rates.append(100.0 * sum(sum(p) for p in pick) / total if total else 0.0)
    rates.sort()
    return round(rates[int(0.025 * iters)], 1), round(rates[int(0.975 * iters) - 1], 1)


def pass_at_k_table(episodes: list[Episode], ks: Iterable[int] = (1, 2, 5, 10)) -> dict[str, float] | None:
    by_task: dict[str, list[bool]] = defaultdict(list)
    for e in episodes:
        if e.grade:
            by_task[e.instance_id].append(bool(e.grade.get("resolved")))
    if not by_task or max(len(v) for v in by_task.values()) < 2:
        return None
    n_min = min(len(v) for v in by_task.values())
    return {f"pass@{k}": round(100 * statistics.fmean(pass_at_k(len(v), sum(v), k) for v in by_task.values()), 1)
            for k in ks if k <= n_min}


def _row(items: list[tuple[bool, float]]) -> dict[str, Any]:
    n = len(items)
    return {"n": n, "resolved_pct": round(100.0 * sum(r for r, _ in items) / n, 1) if n else 0.0,
            "mean_dense": round(statistics.fmean(d for _, d in items), 3) if n else 0.0}


def slices(results: list[dict[str, Any]], tasks: dict[str, Task]) -> dict[str, dict[str, Any]]:
    """Resolve rate and mean dense reward per language, repo, fix size and file count."""
    groups: dict[str, dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in results:
        t = tasks.get(r["instance_id"])
        item = (bool(r.get("resolved")), float(r.get("dense") or 0.0))
        groups["overall"]["all"].append(item)
        if t is None:
            continue
        s = patch_stats(t.gold_patch)
        groups["language"][t.language or "unknown"].append(item)
        groups["repo"][t.repo].append(item)
        groups["gold_lines"][_bucket(s.lines_changed, LINE_BUCKETS)].append(item)
        groups["gold_files"][_bucket(s.files, FILE_BUCKETS)].append(item)
        for cat in t.metadata.get("issue_categories") or []:
            groups["category"][cat].append(item)
    order = {"gold_lines": [b[2] for b in LINE_BUCKETS], "gold_files": [b[2] for b in FILE_BUCKETS]}
    out = {}
    for dim, g in groups.items():
        keys = order.get(dim) or sorted(g, key=lambda k: -len(g[k]))
        out[dim] = {k: _row(g[k]) for k in keys if k in g}
    return out


def build_report(episodes: list[Episode] | None = None, grades: list[dict[str, Any]] | None = None,
                 tasks: list[Task] | None = None, name: str = "") -> dict[str, Any]:
    episodes = episodes or []
    task_map = {t.instance_id: t for t in tasks or []}
    graded_eps = [e for e in episodes if e.grade]
    results = ([{"instance_id": e.instance_id, **e.grade} for e in graded_eps] if episodes
               else list(grades or []))
    rep: dict[str, Any] = {"name": name, "n": len(results), "slices": slices(results, task_map)}
    solved = sum(bool(r.get("resolved")) for r in results)
    rep["resolved_ci95"] = wilson(solved, len(results))
    boot = repo_bootstrap(results, task_map) if task_map else None
    if boot:
        rep["resolved_ci95_repo_bootstrap"] = boot
    if episodes:
        # ungraded episodes (e.g. imported without --grade) count for behaviour, never as failures
        rep["episodes"], rep["ungraded"] = len(episodes), len(episodes) - len(graded_eps)
    if episodes:
        rep["agent"] = sorted({e.agent for e in episodes})
        rep["submitted_pct"] = round(100.0 * sum(e.exit_reason == "submitted" for e in episodes) / len(episodes), 1)
        rep["exit_reasons"] = {k: sum(e.exit_reason == k for e in episodes)
                               for k in sorted({e.exit_reason for e in episodes})}
        rep["mean_turns"] = round(statistics.fmean(e.turns for e in episodes), 1)
        rep["mean_cost"] = round(statistics.fmean(e.cost for e in episodes), 4)
        rep["total_cost"] = round(sum(e.cost for e in episodes), 4)
        rep["budget_curves"] = budget_curves(graded_eps)
        pk = pass_at_k_table(graded_eps)
        if pk:
            rep["pass_at_k"] = pk
        rep["failure_modes"] = distribution(graded_eps, task_map)
    flags = defaultdict(int)
    for r in results:
        for f in r.get("flags") or []:
            flags[f.split(":")[0]] += 1
    rep["flags"] = dict(flags)
    return rep


# ----------------------------------------------------------------- markdown
def _table(header: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(lines)


def to_markdown(reports: list[dict[str, Any]]) -> str:
    """One report, or several side by side (compare agents or budgets)."""
    names = [r.get("name") or (", ".join(r.get("agent", [])) or f"run {i + 1}") for i, r in enumerate(reports)]
    out = ["# swe-env-forge report", ""]
    head = ["metric", *names]
    rows = [["graded episodes", *[r["n"] for r in reports]],
            ["resolved %", *[r["slices"].get("overall", {}).get("all", {}).get("resolved_pct", "n/a") for r in reports]],
            ["95% CI (Wilson)", *[f"{r['resolved_ci95'][0]}–{r['resolved_ci95'][1]}" if r["n"] else "n/a" for r in reports]],
            ["mean dense reward", *[r["slices"].get("overall", {}).get("all", {}).get("mean_dense", "n/a") for r in reports]]]
    if all("budget_curves" in r for r in reports):
        rows += [["ungraded episodes", *[r.get("ungraded", 0) for r in reports]],
                 ["submitted %", *[r["submitted_pct"] for r in reports]],
                 ["mean turns", *[r["mean_turns"] for r in reports]],
                 ["mean cost ($)", *[r["mean_cost"] for r in reports]]]
    out += [_table(head, rows), ""]
    for dim, title in (("language", "By language"), ("gold_lines", "By size of the reference fix (lines changed)"),
                       ("gold_files", "By files in the reference fix"), ("category", "By issue category")):
        keys = []
        for r in reports:
            for k in r["slices"].get(dim, {}):
                if k not in keys:
                    keys.append(k)
        if not keys:
            continue
        out += [f"## {title}", "", _table([dim, *names], [
            [k, *[(lambda c: f"{c['resolved_pct']}% (n={c['n']})" if c else "-")(r["slices"].get(dim, {}).get(k))
                  for r in reports]] for k in keys]), ""]
    if all("budget_curves" in r for r in reports):
        bc = [r["budget_curves"] for r in reports]
        out += ["## Budget curves (resolved % if the run had stopped at a cap)", "",
                _table(["turn cap", *names], [[t, *[b["by_turns"][t] for b in bc]] for t in bc[0]["by_turns"]]), "",
                _table(["cost cap ($)", *names], [[c, *[b["by_cost"][c] for b in bc]] for c in bc[0]["by_cost"]]), "",
                _table(["uncapped", *names], [["", *[b["uncapped"] for b in bc]]]), ""]
        out += ["## Failure modes", ""]
        for name, r in zip(names, reports):
            fm = r["failure_modes"]
            out.append(f"**{name}**: {fm['failures']} failures; submitted {fm['submitted']['pct']}%, "
                       f"not submitted {fm['not_submitted']['pct']}%")
            for side in ("submitted", "not_submitted"):
                for k, v in fm[side]["buckets"].items():
                    out.append(f"- {side.replace('_', ' ')} / {k}: {v['count']} ({v['pct']}%)")
            out.append("")
    if any(r.get("pass_at_k") for r in reports):
        ks = sorted({k for r in reports for k in (r.get("pass_at_k") or {})}, key=lambda k: int(k.split("@")[1]))
        out += ["## pass@k (several samples per task)", "",
                _table(["k", *names], [[k, *[(r.get("pass_at_k") or {}).get(k, "-") for r in reports]] for k in ks]), ""]
    if any(r["flags"] for r in reports):
        out += ["## Grader flags", ""]
        for name, r in zip(names, reports):
            if r["flags"]:
                out.append(f"- {name}: " + ", ".join(f"{k} x{v}" for k, v in sorted(r["flags"].items())))
        out.append("")
    return "\n".join(out)
