"""An environment-setup agent: build, validate, read the errors, repair, repeat.

SWE-bench Pro paid engineers to refine each Dockerfile "until the resulting Docker images
could successfully run the codebase and its tests". This automates that loop:

1. start from the detected plan (:mod:`swe_env_forge.envdetect`) or the task's own Dockerfile;
2. build the image and run :func:`swe_env_forge.validate.validate` on it;
3. if the build fails or the task is not valid, show a model the Dockerfile, the test command
   and the tail of the failing log, and ask for a repaired environment;
4. repeat until the task validates or the attempts run out.

The reward is the validation verdict itself: the reference fix must make the tests pass, the
empty patch must not, and there must be at least one fail-to-pass test. The model never sees
the reference fix or the hidden tests' expectations, and every proposal is checked before it
is used (the repository must still be checked out at the base commit, the parser must exist).
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Callable

from .logparsers import PARSERS
from .task import Task

SETUP_PROMPT = """You are a build engineer. A repository must run its test suite inside Docker so a benchmark task
can be validated. The last attempt failed. Propose a repaired environment.

Rules:
- Keep the line that clones the repository and checks out commit {base} exactly as it is.
- Change system packages, language versions, install steps, environment variables or the test command.
- The test command must print one result line per test that the parser "{parser}" understands; keep the
  {{test_files}} placeholder if the current command has it.
- Prefer the repository's own tooling (its requirements files, lockfiles, CI workflow).

Reply with one JSON object only:
{{"dockerfile": "<full Dockerfile>", "test_command": "<command>", "log_parser": "<parser name>",
  "explanation": "<one sentence>"}}

Known parsers: {parsers}"""


@dataclass
class SetupAttempt:
    n: int
    outcome: str                      # build_failed | invalid | valid | rejected_proposal
    reasons: list[str] = field(default_factory=list)
    dockerfile: str = ""
    test_command: str = ""
    log_parser: str = ""
    explanation: str = ""
    log_tail: str = ""


def _extract_json(text: str) -> dict[str, Any] | None:
    from .spec import _json_object

    return _json_object(text)


def check_proposal(task: Task, proposal: dict[str, Any]) -> str:
    """'' when the proposed environment is acceptable, else the reason it is rejected."""
    df = str(proposal.get("dockerfile") or "")
    if not df.lstrip().upper().startswith(("FROM", "ARG", "#")) or "FROM" not in df.upper():
        return "dockerfile must start with FROM"
    if task.base_commit not in df:
        return "dockerfile must still check out the base commit"
    if proposal.get("log_parser") and proposal["log_parser"] not in PARSERS:
        return f"unknown log parser {proposal['log_parser']!r}"
    cmd = str(proposal.get("test_command") or "")
    if not cmd.strip():
        return "test command is empty"
    if "{test_files}" in (task.env.test_command or "") and "{test_files}" not in cmd and task.env.test_file_format != "none":
        return "test command dropped the {test_files} placeholder"
    return ""


def setup_environment(task: Task, client=None, *, max_attempts: int = 4, runs: int = 1, backend: str = "docker",
                      build: Callable[[Task], str] | None = None,
                      check: Callable[[Task], Any] | None = None,
                      max_asks: int = 3) -> tuple[Task, list[SetupAttempt]]:
    """Make ``task`` validate, repairing its environment with ``client`` when needed.

    Each attempt builds and validates once. When it fails, the model is asked for a repair; an
    unusable reply is sent back with the reason (up to ``max_asks`` times) without rebuilding.
    ``build`` and ``check`` default to building the Docker image and running validation; tests
    inject fakes. Returns the task (with the last environment tried) and the attempt history.
    """
    from .envdetect import apply_plan, detect_for_task
    from .validate import apply_validation, validate
    from .workspace import ensure_image

    build = build or (lambda t: ensure_image(t, rebuild=True))
    check = check or (lambda t: validate(t, backend, runs=runs))
    if not task.env.image and not task.env.dockerfile:
        apply_plan(task, detect_for_task(task))
    context = _repo_context(task)
    attempts: list[SetupAttempt] = []
    for n in range(1, max_attempts + 1):
        att = SetupAttempt(n, "", dockerfile=task.env.dockerfile or "", test_command=task.env.test_command or "",
                           log_parser=task.env.log_parser)
        try:
            build(task)
        except Exception as exc:
            att.outcome, att.reasons, att.log_tail = "build_failed", ["build_failed"], str(exc)[-4000:]
        else:
            res = check(task)
            if res.verdict == "valid":
                att.outcome = "valid"
                attempts.append(att)
                apply_validation(task, res)
                break
            att.outcome, att.reasons = "invalid", list(res.reasons)
            tails = res.log_tails or {}
            att.log_tail = "\n".join(tails.get(k, "")[-2500:] for k in ("post0", "pre0") if tails.get(k))
        attempts.append(att)
        if client is None or n == max_attempts:
            break
        messages = _first_request(task, att, context)
        proposal = None
        for _ask_no in range(max_asks):
            text, _usage = client.chat(messages)
            proposal = parse_proposal(text, task)
            why = check_proposal(task, proposal) if proposal else "no Dockerfile in the reply"
            if not why:
                break
            attempts.append(SetupAttempt(n, "rejected_proposal", [why], explanation=text.strip()[:300]))
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": f"That reply cannot be used: {why}. Reply again with ONLY the JSON "
                                                     "object, or a ```dockerfile block followed by a line "
                                                     "'TEST_COMMAND: <command>'."}]
            proposal = None
        if proposal is None:
            continue  # rebuild the same environment once more; the log may differ (flaky installs)
        task.env.dockerfile = str(proposal["dockerfile"])
        task.env.test_command = str(proposal["test_command"])
        if proposal.get("log_parser"):
            task.env.log_parser = str(proposal["log_parser"])
        att.explanation = str(proposal.get("explanation", ""))
    task.metadata["env_setup"] = {"attempts": [asdict(a) | {"dockerfile": "", "log_tail": ""} for a in attempts],
                                  "valid": bool(attempts and attempts[-1].outcome == "valid")}
    return task, attempts


_DEP_FILES = ("requirements.txt", "requirements-dev.txt", "requirements-test.txt", "requirements_test.txt",
              "test-requirements.txt", "tox.ini", "setup.cfg", "pyproject.toml", "package.json", "go.mod",
              "Cargo.toml", "Gemfile", "composer.json", ".python-version", ".nvmrc")


def _repo_context(task: Task, max_chars: int = 5000) -> str:
    """Dependency files at the base commit (names and heads), so the model can see what tests need."""
    try:
        from .envdetect import RepoView
        from .workspace import repo_cache

        view = RepoView(repo_cache(task.clone_url, task.base_commit), task.base_commit)
    except Exception:
        return ""
    files = [f for f in _DEP_FILES if view.exists(f)] + view.glob("requirements/*.txt")
    out, used = [], 0
    for f in files:
        text = "\n".join((view.read(f) or "").splitlines()[:40])
        chunk = f"--- {f} ---\n{text}\n"
        if used + len(chunk) > max_chars:
            out.append(f"--- {f} --- (not shown)")
            continue
        out.append(chunk)
        used += len(chunk)
    return "\n".join(out)


def _first_request(task: Task, att: SetupAttempt, context: str) -> list[dict[str, str]]:
    system = SETUP_PROMPT.format(base=task.base_commit, parser=task.env.log_parser,
                                 parsers=", ".join(sorted(PARSERS)))
    evidence = "\n".join((task.metadata.get("env_plan") or {}).get("evidence", [])[:12])
    log_tail = re.sub(r"[ \t]+", " ", att.log_tail)[-6000:]
    msg = (f"Repository: {task.repo} ({task.language})\nWhat we learned from the repo:\n{evidence or '(nothing)'}\n\n"
           f"Dependency files in the repository at this commit:\n{context or '(none found)'}\n\n"
           f"Current Dockerfile:\n{task.env.dockerfile}\n\nCurrent test command: {task.env.test_command}\n"
           f"Log parser: {task.env.log_parser}\n\nOutcome: {att.outcome} {att.reasons}\n"
           f"Log tail:\n{log_tail}")
    return [{"role": "system", "content": system}, {"role": "user", "content": msg}]


_DOCKER_BLOCK = re.compile(r"```(?:dockerfile|docker|Dockerfile)?\s*\n(FROM.*?)```", re.S | re.I)
_TEST_LINE = re.compile(r"^\W*TEST[_ ]COMMAND\W*:?\s*`?(.+?)`?\s*$", re.M | re.I)


def parse_proposal(text: str, task: Task) -> dict[str, Any] | None:
    """A proposal from JSON, or from a ```dockerfile block plus a TEST_COMMAND line (smaller models)."""
    obj = _extract_json(text)
    if obj and obj.get("dockerfile"):
        obj.setdefault("test_command", task.env.test_command or "")
        return obj
    m = _DOCKER_BLOCK.search(text)
    if not m:
        return None
    t = _TEST_LINE.search(text)
    return {"dockerfile": m.group(1).strip() + "\n", "test_command": t.group(1).strip() if t else (task.env.test_command or ""),
            "explanation": "markdown reply"}


def summary(attempts: list[SetupAttempt]) -> str:
    return " -> ".join(f"{a.outcome}" + (f"({','.join(a.reasons)})" if a.reasons else "") for a in attempts)


__all__ = ["setup_environment", "check_proposal", "SetupAttempt", "summary"]
