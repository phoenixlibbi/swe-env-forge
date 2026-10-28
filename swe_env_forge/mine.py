"""Mine candidate tasks from a repository's history.

Two strategies, one per paper:

* ``commits`` (SWE-bench Pro): every first-parent commit is a (base, instance) pair. The
  diff between them is split into a test patch and a gold patch. Works offline and on
  private repositories; the commit message is only a draft problem statement.
* ``prs`` (SWE-bench): merged pull requests that reference an issue with a closing
  keyword and touch tests. The issue text becomes the problem statement, and issue
  comments written before the PR's first commit become hints.

Candidates still need ``validate`` (and, for commit pairs, ``spec`` + human review).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import presets
from .hub import GitHub
from .languages import detect_language
from .patches import files_in, patch_stats, split_patch
from .task import EnvSpec, Task
from .workspace import repo_cache

CLOSING_RE = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s*:?\s+(?:[\w.-]+/[\w.-]+)?#(\d+)\b", re.I)


@dataclass
class MineConfig:
    min_lines: int = 1             # gold patch lines changed (Pro uses 10)
    max_lines: int | None = None
    min_code_files: int = 1        # source files touched by the gold patch
    max_files: int | None = None
    require_tests: bool = True
    max_tasks: int | None = None   # per repository (Pro caps at 100)
    since: str | None = None
    until: str | None = None
    branch: str = "HEAD"
    max_commits: int | None = None
    # Direct-push repositories often split one change over several commits (code today, tests
    # tomorrow). window > 1 lets a commit that is not a task on its own merge with up to
    # window-1 neighbouring commits; group_by limits merges to one author and/or one day.
    window: int = 1
    group_by: str = "none"          # none | author | day | author-day

    @classmethod
    def pro(cls, **kw) -> "MineConfig":
        """SWE-bench Pro's filters: at least 10 changed lines, at most 100 tasks per repo."""
        return cls(**{"min_lines": 10, "max_tasks": 100, **kw})


def keep(gold: str, tests: str, cfg: MineConfig) -> tuple[bool, str]:
    """Whether a (gold, tests) split passes the filters, with the reason when it does not."""
    if cfg.require_tests and not tests.strip():
        return False, "no_test_changes"
    s = patch_stats(gold)
    if s.code_files < cfg.min_code_files:
        return False, "no_code_changes"
    if s.lines_changed < cfg.min_lines:
        return False, "too_small"
    if cfg.max_lines is not None and s.lines_changed > cfg.max_lines:
        return False, "too_large"
    if cfg.max_files is not None and s.files > cfg.max_files:
        return False, "too_many_files"
    return True, ""


def default_env(language: str) -> EnvSpec:
    tpl = presets.LANGUAGE_TEMPLATES.get(language)
    if not tpl:
        return EnvSpec(log_parser="auto")
    return EnvSpec(test_command=tpl["test_command"], log_parser=tpl["log_parser"],
                   test_file_format=tpl["test_file_format"])


def _slug(repo: str) -> str:
    return repo.replace("/", "__")


# ------------------------------------------------------------------- commits
def repo_name_from_url(url: str) -> str:
    """'owner/name' from https://host/owner/name(.git) or git@host:owner/name(.git)."""
    path = re.sub(r"\.git/?$", "", url.rstrip("/"))
    parts = re.split(r"[/:]", path)
    return "/".join(p for p in parts[-2:] if p)
def _git(path: Path, *args: str) -> str:
    res = subprocess.run(["git", "-C", str(path), *args], capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {res.stderr.strip()[-300:]}")
    return res.stdout


def _group_key(meta: dict[str, str], group_by: str) -> tuple:
    parts = []
    if "author" in group_by:
        parts.append(meta["author"])
    if "day" in group_by:
        parts.append(meta["date"][:10])
    return tuple(parts)


def mine_commits(source: str, repo: str | None = None, cfg: MineConfig | None = None,
                 on_skip: Callable[[str, str], None] | None = None) -> list[Task]:
    """Mine (base, instance) commit pairs from a local path or clone URL, newest first.

    With ``cfg.window > 1``, a commit that is not a task by itself (for example code without
    tests) is merged with up to ``window - 1`` earlier first-parent commits until the combined
    diff qualifies; the task then spans base = parent of the first commit, instance = the last.
    Each commit is used by at most one task.
    """
    cfg = cfg or MineConfig()
    is_local = Path(source).expanduser().exists()
    path = Path(source).expanduser().resolve() if is_local else repo_cache(source)
    repo = repo or (path.name if is_local else repo_name_from_url(source))
    args = ["rev-list", "--first-parent"]
    if cfg.since:
        args.append(f"--since={cfg.since}")
    if cfg.until:
        args.append(f"--until={cfg.until}")
    if cfg.max_commits:
        args.append(f"--max-count={cfg.max_commits}")
    args.append(cfg.branch)
    shas = _git(path, *args).split()            # newest first
    meta: dict[str, dict[str, str]] = {}
    for line in _git(path, "log", "--first-parent", "--format=%H%x00%P%x00%an%x00%cI", *args[2:]).splitlines():
        parts = line.split("\x00")
        if len(parts) == 4:
            meta[parts[0]] = {"parent": parts[1].split()[0] if parts[1] else "", "author": parts[2], "date": parts[3]}
    used: set[str] = set()
    tasks: list[Task] = []
    for i, sha in enumerate(shas):
        if sha in used or not meta.get(sha, {}).get("parent"):
            continue
        span = [sha]
        why = ""
        for k in range(max(1, cfg.window)):
            first = span[-1]
            base = meta[first]["parent"]
            diff = _git(path, "diff", "--no-color", "--no-ext-diff", "-M", base, sha)
            gold, tests = split_patch(diff)
            ok, why = keep(gold, tests, cfg)
            if ok or why in ("too_large", "too_many_files"):
                break
            nxt = i + k + 1                     # the next older first-parent commit
            if nxt >= len(shas) or shas[nxt] in used or not meta.get(shas[nxt], {}).get("parent"):
                break
            if cfg.group_by != "none" and _group_key(meta[shas[nxt]], cfg.group_by) != _group_key(meta[sha], cfg.group_by):
                break
            span.append(shas[nxt])
        if not ok:
            if on_skip:
                on_skip(sha, why)
            continue
        used.update(span)
        from .curate import strip_trailers   # Signed-off-by / Co-authored-by lines carry names and emails

        messages = [strip_trailers(_git(path, "log", "-1", "--format=%B", c)) for c in reversed(span)]
        language = detect_language(files_in(gold) + files_in(tests))
        tasks.append(Task(
            instance_id=f"{_slug(repo)}-{sha[:12]}", repo=repo, base_commit=base,
            problem_statement="\n\n".join(messages), gold_patch=gold, test_patch=tests, language=language,
            source="mined-commits", created_at=meta[sha]["date"],
            repo_url=str(path) if is_local else source, env=default_env(language),
            metadata={"instance_commit": sha, "commits": list(reversed(span)), "needs_augmentation": True},
        ))
        if cfg.max_tasks and len(tasks) >= cfg.max_tasks:
            break
    return tasks


# ----------------------------------------------------------------------- PRs
def resolved_issues(text: str) -> list[int]:
    return sorted({int(n) for n in CLOSING_RE.findall(text or "")})


def mine_prs(repo: str, cfg: MineConfig | None = None, gh: GitHub | None = None, max_prs: int = 500,
             on_skip: Callable[[str, str], None] | None = None) -> list[Task]:
    """Mine merged PRs of ``owner/name`` that close an issue and change tests (SWE-bench rules)."""
    cfg, gh = cfg or MineConfig(), gh or GitHub()
    tasks: list[Task] = []
    pulls: Iterable[dict] = gh.paginate(f"/repos/{repo}/pulls",
                                         {"state": "closed", "sort": "created", "direction": "desc"}, limit=max_prs)
    for pr in pulls:
        num = pr["number"]
        if not pr.get("merged_at"):
            continue
        issues = [i for i in resolved_issues(f"{pr.get('title', '')}\n{pr.get('body') or ''}") if i != num]
        if not issues:
            on_skip and on_skip(str(num), "no_linked_issue")
            continue
        diff = gh.get(f"/repos/{repo}/pulls/{num}", accept="application/vnd.github.v3.diff")
        gold, tests = split_patch(diff)
        ok, why = keep(gold, tests, cfg)
        if not ok:
            on_skip and on_skip(str(num), why)
            continue
        commits = gh.get(f"/repos/{repo}/pulls/{num}/commits", {"per_page": 1})
        first_commit = (commits[0]["commit"]["author"]["date"] if commits else pr["created_at"])
        statements, hints = [], []
        for i in issues:
            issue = gh.get(f"/repos/{repo}/issues/{i}")
            if issue.get("pull_request"):
                continue  # "fixes #N" pointing at another PR
            statements.append(f"{issue.get('title', '')}\n{issue.get('body') or ''}".strip())
            for c in gh.paginate(f"/repos/{repo}/issues/{i}/comments", limit=200):
                if c.get("created_at", "") < first_commit:
                    hints.append(c.get("body") or "")
        if not statements:
            on_skip and on_skip(str(num), "linked_issue_is_a_pr")
            continue
        language = detect_language(files_in(gold) + files_in(tests))
        tasks.append(Task(
            instance_id=f"{_slug(repo)}-{num}", repo=repo, base_commit=pr["base"]["sha"],
            problem_statement="\n\n".join(statements), hints="\n\n".join(hints), gold_patch=gold,
            test_patch=tests, language=language, source="mined-pr", created_at=pr.get("created_at", ""),
            env=default_env(language),
            metadata={"pr_number": num, "issue_numbers": issues, "merge_commit": pr.get("merge_commit_sha")},
        ))
        if cfg.max_tasks and len(tasks) >= cfg.max_tasks:
            break
    return tasks
