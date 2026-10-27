"""File localization baselines from the SWE-bench paper: BM25 retrieval vs the oracle.

SWE-bench's original baselines did not let the model search. A BM25 retriever ranked
the repository's files against the issue text (file paths prepended to contents) and
filled the prompt up to 13k / 27k / 50k tokens. The "oracle" setting instead gave the
model exactly the files the reference patch edits. This module reproduces both and
measures how often BM25 finds the oracle files (the paper's Table 3), plus rank metrics.
It reads files straight from git objects, so no checkout or Docker is needed.
"""

from __future__ import annotations

import math
import re
import statistics
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .languages import is_code_path, is_test_path
from .patches import parse_patch
from .task import Task
from .workspace import repo_cache

DEFAULT_BUDGETS = (13_000, 27_000, 50_000)
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z0-9]+|[A-Z]+|\d+")
_SPLIT = re.compile(r"[A-Za-z0-9]+")


def tokenize(text: str) -> list[str]:
    """Code-aware tokens: identifiers are split on case and underscores, lower-cased."""
    out = []
    for chunk in _SPLIT.findall(text):
        parts = _WORD.findall(chunk)
        if len(parts) > 1:
            out.append(chunk.lower())
        out.extend(p.lower() for p in parts if len(p) > 1)
    return out


_ENCODER: Any = None


def count_tokens(text: str) -> int:
    """Approximate model tokens (about 4 characters each); uses tiktoken when installed."""
    global _ENCODER
    if _ENCODER is None:
        try:
            import tiktoken  # type: ignore

            _ENCODER = tiktoken.get_encoding("cl100k_base")
        except Exception:
            _ENCODER = False
    if _ENCODER:
        return len(_ENCODER.encode(text, disallowed_special=()))
    return max(1, len(text) // 4)


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.2, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.lengths = [len(d) for d in docs]
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i, d in enumerate(docs):
            for term, tf in Counter(d).items():
                self.postings[term].append((i, tf))

    def idf(self, term: str) -> float:
        df = len(self.postings.get(term, ()))
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def scores(self, query: list[str]) -> list[float]:
        out = [0.0] * self.n
        for term in set(query):
            idf = self.idf(term)
            for i, tf in self.postings.get(term, ()):
                norm = tf + self.k1 * (1 - self.b + self.b * self.lengths[i] / (self.avgdl or 1))
                out[i] += idf * tf * (self.k1 + 1) / norm
        return out


# ------------------------------------------------------------------ repo files
def read_files(repo_dir: str | Path, commit: str, include_tests: bool = False, max_bytes: int = 500_000,
               code_only: bool = True) -> dict[str, str]:
    """{path: text} for files at ``commit`` straight from git objects (no checkout)."""
    ls = subprocess.run(["git", "-C", str(repo_dir), "ls-tree", "-r", "-l", "--full-tree", commit],
                        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if ls.returncode != 0:
        raise RuntimeError(f"git ls-tree {commit}: {ls.stderr.strip()[-300:]}")
    wanted = []
    for line in ls.stdout.splitlines():
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) < 4 or parts[1] != "blob" or not parts[3].isdigit() or int(parts[3]) > max_bytes:
            continue
        if code_only and not is_code_path(path):
            continue
        if not include_tests and is_test_path(path):
            continue
        wanted.append((parts[2], path))
    files: dict[str, str] = {}
    if not wanted:
        return files
    proc = subprocess.run(["git", "-C", str(repo_dir), "cat-file", "--batch"],
                          input="".join(f"{sha}\n" for sha, _ in wanted).encode(), capture_output=True)
    data, pos = proc.stdout, 0
    for _sha, path in wanted:
        nl = data.index(b"\n", pos)
        header = data[pos:nl].split()
        size = int(header[2])
        blob = data[nl + 1:nl + 1 + size]
        pos = nl + 1 + size + 1
        if b"\x00" in blob[:8000]:
            continue
        files[path] = blob.decode("utf-8", "replace")
    return files


def oracle_files(task: Task) -> list[str]:
    """Files the reference patch edits that already existed at the base commit."""
    return [f.old_path or f.path for f in parse_patch(task.gold_patch) if not f.added_file]


@dataclass
class Localization:
    instance_id: str
    oracle: list[str]
    ranking: list[str] = field(default_factory=list)       # top files by BM25
    by_budget: dict[str, dict[str, Any]] = field(default_factory=dict)
    first_hit_rank: int | None = None                      # 1-based rank of the first oracle file
    recall_at: dict[str, float] = field(default_factory=dict)
    n_files: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def rank_files(files: dict[str, str], query: str, k1: float = 1.2, b: float = 0.75) -> list[str]:
    paths = list(files)
    bm = BM25([tokenize(p + "\n" + files[p]) for p in paths], k1, b)
    scores = bm.scores(tokenize(query))
    return [paths[i] for i in sorted(range(len(paths)), key=lambda i: (-scores[i], paths[i]))]


def fill_budget(ranking: list[str], files: dict[str, str], budget: int) -> list[str]:
    """Top-ranked files, in order, until the next one would exceed ``budget`` tokens."""
    chosen, used = [], 0
    for p in ranking:
        cost = count_tokens(f"[start of {p}]\n{files[p]}\n[end of {p}]\n")
        if used + cost > budget:
            break
        chosen.append(p)
        used += cost
    return chosen


def localize_task(task: Task, budgets: Iterable[int] = DEFAULT_BUDGETS, files: dict[str, str] | None = None,
                  include_tests: bool = False, top: int = 20) -> Localization:
    if files is None:
        repo = repo_cache(task.clone_url, task.base_commit)
        files = read_files(repo, task.base_commit, include_tests=include_tests)
    oracle = oracle_files(task)
    ranking = rank_files(files, task.problem_statement)
    loc = Localization(task.instance_id, oracle, ranking[:top], n_files=len(files))
    pos = {p: i + 1 for i, p in enumerate(ranking)}
    hits = [pos[o] for o in oracle if o in pos]
    loc.first_hit_rank = min(hits) if hits else None
    for k in (1, 5, 10):
        loc.recall_at[str(k)] = round(sum(1 for h in hits if h <= k) / len(oracle), 4) if oracle else 0.0
    for budget in budgets:
        chosen = set(fill_budget(ranking, files, budget))
        found = [o for o in oracle if o in chosen]
        loc.by_budget[str(budget)] = {
            "files": len(chosen), "found": len(found),
            "all": bool(oracle) and len(found) == len(oracle), "any": bool(found),
            "avg": round(len(found) / len(oracle), 4) if oracle else 0.0,
        }
    return loc


def summarize(locs: list[Localization]) -> dict[str, Any]:
    """The paper's Table 3 (average / all / any recall per budget) plus rank metrics."""
    locs = [l for l in locs if l.oracle]
    n = len(locs)
    if not n:
        return {"tasks": 0}
    out: dict[str, Any] = {"tasks": n, "budgets": {}}
    for b in locs[0].by_budget:
        rows = [l.by_budget[b] for l in locs]
        out["budgets"][b] = {"avg": round(100 * statistics.fmean(r["avg"] for r in rows), 2),
                             "all": round(100 * sum(r["all"] for r in rows) / n, 2),
                             "any": round(100 * sum(r["any"] for r in rows) / n, 2),
                             "none": round(100 * sum(not r["any"] for r in rows) / n, 2)}
    out["recall_at"] = {k: round(100 * statistics.fmean(l.recall_at[k] for l in locs), 2) for k in locs[0].recall_at}
    ranks = [l.first_hit_rank for l in locs]
    out["mrr"] = round(statistics.fmean((1 / r) if r else 0.0 for r in ranks), 4)
    out["median_first_hit_rank"] = statistics.median([r for r in ranks if r]) if any(ranks) else None
    return out


# -------------------------------------------------------------------- prompts
PROMPT_TEMPLATE = """You will be provided with a partial code base and an issue statement explaining a problem to resolve.
<issue>
{issue}
</issue>
<code>
{code}
</code>
Here is an example of a patch file. It consists of changes to the code base. It specifies the file names, the line numbers of each change, and the removed and added lines. A single patch file can contain changes to multiple files.
<patch>
--- a/file.py
+++ b/file.py
@@ -1,3 +1,3 @@
 def euclidean(a, b):
-    while b:
-        a, b = b, a % b
+    if b == 0:
+        return a
+    return euclidean(b, a % b)
</patch>
I need you to solve the provided issue by generating a single patch file that I can apply directly to this repository using git apply. Please respond with a single patch file in the format shown above.
Respond below:"""


def build_prompt(task: Task, files: dict[str, str], chosen: list[str], line_numbers: bool = True) -> str:
    """A SWE-bench style one-shot prompt over the chosen files."""
    blocks = []
    for p in chosen:
        text = files[p]
        if line_numbers:
            text = "\n".join(f"{i} {ln}" for i, ln in enumerate(text.splitlines(), 1))
        blocks.append(f"[start of {p}]\n{text}\n[end of {p}]")
    return PROMPT_TEMPLATE.format(issue=task.problem_statement.strip(), code="\n".join(blocks))


_PATCH_BLOCK = re.compile(r"<patch>\s*(.*?)\s*</patch>|```(?:diff|patch)?\s*\n(.*?)```", re.S)


def extract_patch(reply: str) -> str:
    """Pull a diff out of a model reply (``<patch>`` tags or a fenced block)."""
    m = _PATCH_BLOCK.search(reply)
    text = (m.group(1) or m.group(2)) if m else reply
    start = re.search(r"^(diff --git|--- )", text, re.M)
    return text[start.start():].rstrip() + "\n" if start else ""
