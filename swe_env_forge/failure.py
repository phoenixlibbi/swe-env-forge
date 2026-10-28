"""Why did an episode fail? A deterministic classifier using SWE-bench Pro's failure buckets.

Pro splits failures into *submitted* (wrong solution, syntax error, incorrect file,
instruction following, edge case, other) and *not submitted* (tool use, long context,
stuck in a loop), using an LLM judge on the last 20 turns. This module assigns the same
kind of buckets from hard evidence (exit reason, grade, patch, commands) so it is free,
fast and reproducible. :func:`llm_judge` offers the Pro-style judge as an option.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

from .patches import files_in
from .task import Task
from .trajectory import Episode

SUBMITTED_BUCKETS = ("wrong_solution", "edge_case", "regression", "syntax_error", "incorrect_file",
                     "instruction_following", "empty_patch", "malformed_patch", "other")
NOT_SUBMITTED_BUCKETS = ("long_context", "stuck_in_loop", "endless_reading", "tool_use", "budget_exhausted",
                         "agent_error", "harness_error")

_SYNTAX = re.compile(r"\bSyntaxError\b|\bIndentationError\b|\bTabError\b|error TS1\d{3}|error\[E0(?:06[0-9]|1\d\d)\]"
                     r"|syntax error|expected ';'|Unexpected token|ParseError|could not compile|compilation failed"
                     r"|error CS1\d{3}|unexpected EOF", re.I)
_READ_ONLY = re.compile(r"^\s*(cat|head|tail|less|more|grep|rg|ag|find|ls|tree|wc|sed\s+-n|nl|awk|file|stat|"
                        r"str_replace_editor\s+view|open|goto|scroll_(?:up|down)|search_\w+|find_file|git\s+(?:log|show|diff|status|grep))\b")


def _commands(ep: Episode) -> list[str]:
    return [str(s.action.get("command", "")).strip() for s in ep.steps if s.action.get("type") == "bash"]


def stuck_in_loop(commands: list[str], window: int = 3) -> bool:
    """The same command repeated ``window`` times in a row, or a short cycle repeated."""
    for i in range(len(commands) - window + 1):
        if len(set(commands[i:i + window])) == 1 and commands[i]:
            return True
    tail = commands[-12:]
    for period in (2, 3):
        if len(tail) >= period * 3 and all(tail[-k] == tail[-k - period] for k in range(1, period * 2 + 1)):
            return True
    return False


def classify(ep: Episode, task: Task | None = None) -> str:
    """One failure bucket for an unresolved episode ('' when resolved)."""
    g = ep.grade or {}
    if g.get("resolved"):
        return ""
    if ep.exit_reason == "env_error" or g.get("failure") == "harness_error":
        return "harness_error"
    commands = _commands(ep)
    if ep.exit_reason != "submitted":
        if ep.exit_reason == "context_overflow":
            return "long_context"
        if ep.exit_reason == "agent_error":
            return "agent_error"
        if stuck_in_loop(commands):
            return "stuck_in_loop"
        tail_steps = [s for s in ep.steps if s.action.get("type") == "bash"][-20:]
        if tail_steps:
            errors = sum(1 for s in tail_steps if s.exit_code != 0) / len(tail_steps)
            reads = sum(1 for s in tail_steps if _READ_ONLY.match(str(s.action.get("command", "")))) / len(tail_steps)
            if errors > 0.5:
                return "tool_use"
            if reads >= 0.7 and not ep.patch.strip():
                return "endless_reading"
        return "budget_exhausted"
    failure = g.get("failure", "")
    if failure == "empty_patch" or not ep.patch.strip():
        return "empty_patch"
    if failure == "patch_apply_error":
        return "malformed_patch"
    if failure == "tampering" or any(f.startswith(("edits_hidden_test_file", "adds_skip_marker", "deletes_test_file",
                                                   "edits_test_config", "blocks_hidden_tests",
                                                   "touches_test_framework", "framework_compromised",
                                                   "conflicting_test_status"))
                                     for f in g.get("flags", [])):
        return "instruction_following"
    if _SYNTAX.search(g.get("test_log_tail", "")):
        return "syntax_error"
    if task is not None:
        gold_files = set(files_in(task.gold_patch))
        if gold_files and not gold_files & set(files_in(ep.patch)):
            return "incorrect_file"
    if failure == "regression":
        return "regression"
    if failure == "partial":
        return "edge_case"
    if failure in ("wrong_solution", "tests_not_run"):
        return "wrong_solution"
    return "other"


def distribution(episodes: list[Episode], tasks: dict[str, Task] | None = None) -> dict[str, Any]:
    tasks = tasks or {}
    sub, nsub = Counter(), Counter()
    for ep in episodes:
        b = classify(ep, tasks.get(ep.instance_id))
        if not b:
            continue
        (sub if ep.exit_reason == "submitted" else nsub)[b] += 1
    n_fail = sum(sub.values()) + sum(nsub.values())
    pct = lambda c, d: round(100.0 * c / d, 1) if d else 0.0  # noqa: E731
    return {
        "failures": n_fail,
        "submitted": {"count": sum(sub.values()), "pct": pct(sum(sub.values()), n_fail),
                      "buckets": {k: {"count": v, "pct": pct(v, sum(sub.values()))} for k, v in sub.most_common()}},
        "not_submitted": {"count": sum(nsub.values()), "pct": pct(sum(nsub.values()), n_fail),
                          "buckets": {k: {"count": v, "pct": pct(v, sum(nsub.values()))} for k, v in nsub.most_common()}},
    }


JUDGE_PROMPT = """You are an expert software engineer analysing why a software engineering agent failed to resolve an issue.
Exit status: {exit_reason}. Grade: {failure}.
Choose exactly one bucket.
If the agent submitted: wrong_solution, syntax_error, incorrect_file, instruction_following, edge_case, other.
If it did not submit: tool_use, long_context, stuck_in_loop.
Reply with one paragraph of reasoning, then a final line `BUCKET: <name>`.

Last {n} turns:
{turns}"""


def llm_judge(ep: Episode, client, last_n: int = 20, max_chars: int = 60_000) -> dict[str, str]:
    """Pro's LLM-as-a-judge over the last ``last_n`` turns of a failed trajectory."""
    turns = []
    for s in ep.steps[-last_n:]:
        turns.append(f"[turn {s.turn}] THOUGHT: {s.thought[:500]}\nACTION: {str(s.action.get('command', s.action))[:1500]}\n"
                     f"OUTPUT (exit {s.exit_code}): {s.output[:2000]}")
    body = "\n\n".join(turns)[-max_chars:]
    g = ep.grade or {}
    text, _ = client.chat([{"role": "user", "content": JUDGE_PROMPT.format(
        exit_reason=ep.exit_reason, failure=g.get("failure", "unknown"), n=last_n, turns=body)}])
    m = re.search(r"BUCKET:\s*([a-z_]+)", text)
    return {"bucket": m.group(1) if m else "other", "reasoning": text.strip()}
