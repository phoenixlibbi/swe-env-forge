"""Task-factory hygiene: duplicate detection and secret / personal-data scanning.

* :func:`find_duplicates` groups tasks whose fixes are the same change: an exact match on a
  normalized diff, or a near match on line shingles (Jaccard). It catches a PR task and its
  commit-pair twin, a cherry-pick on another branch, or a task that already exists in a public
  benchmark (pass that benchmark as ``against``).
* :func:`scan_task` finds credentials (cloud keys, tokens, private keys, JWTs, high-entropy
  values next to secret-like names) and personal data (email addresses, ``Signed-off-by``
  trailers) in every field. :func:`redact_task` cleans the agent-visible text.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from .patches import parse_patch
from .task import Task

# --------------------------------------------------------------------- duplicates
_WS = re.compile(r"\s+")


def _norm_lines(patch: str) -> list[str]:
    """Changed lines of a diff with whitespace collapsed, independent of line numbers and file order."""
    out = []
    for f in sorted(parse_patch(patch), key=lambda f: f.path):
        out += ["+" + _WS.sub(" ", ln).strip() for ln in f.added if ln.strip()]
        out += ["-" + _WS.sub(" ", ln).strip() for ln in f.removed if ln.strip()]
    return out


def fingerprint(task: Task) -> str:
    return hashlib.sha256("\n".join(_norm_lines(task.gold_patch)).encode()).hexdigest()


def _shingles(lines: list[str], k: int = 3) -> set[str]:
    if len(lines) <= k:
        return {"\n".join(lines)} if lines else set()
    return {"\n".join(lines[i:i + k]) for i in range(len(lines) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


@dataclass
class Duplicate:
    instance_id: str
    duplicate_of: str
    similarity: float
    kind: str          # exact | near | external


def find_duplicates(tasks: list[Task], threshold: float = 0.8, against: Iterable[Task] = ()) -> list[Duplicate]:
    """Later tasks that repeat an earlier one (or a task in ``against``)."""
    seen: list[tuple[str, str, set[str], bool]] = []   # (id, fingerprint, shingles, external)
    for t in against:
        seen.append((t.instance_id, fingerprint(t), _shingles(_norm_lines(t.gold_patch)), True))
    dups: list[Duplicate] = []
    for t in tasks:
        fp, sh = fingerprint(t), _shingles(_norm_lines(t.gold_patch))
        best: Duplicate | None = None
        for iid, ofp, osh, external in seen:
            if fp == ofp and sh:
                best = Duplicate(t.instance_id, iid, 1.0, "external" if external else "exact")
                break
            sim = jaccard(sh, osh)
            if sim >= threshold and (best is None or sim > best.similarity):
                best = Duplicate(t.instance_id, iid, round(sim, 3), "external" if external else "near")
        if best:
            dups.append(best)
        else:
            seen.append((t.instance_id, fp, sh, False))
    return dups


# ------------------------------------------------------------------------ secrets
_SECRET_PATTERNS = [
    ("aws_access_key", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("github_token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b"),
    ("slack_token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    ("google_api_key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("stripe_live_key", r"\b(?:sk|rk)_live_[0-9A-Za-z]{20,}\b"),
    ("openai_key", r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{32,}\b"),
    ("anthropic_key", r"\bsk-ant-[A-Za-z0-9_\-]{32,}\b"),
    ("private_key", r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY-----"),
    ("jwt", r"\beyJ[A-Za-z0-9_\-]{10,}\.eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b"),
]
SECRET_RES = [(n, re.compile(p)) for n, p in _SECRET_PATTERNS]
_ASSIGN_RE = re.compile(r"""(?i)\b[\w.-]*(?:secret|token|passw(?:or)?d|api[_-]?key|access[_-]?key|auth)[\w.-]*\s*[:=]\s*['"]([^'"\s]{16,})['"]""")
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
TRAILER_RE = re.compile(r"^(?:Signed-off-by|Co-authored-by|Reviewed-by|Acked-by|Reported-by|Tested-by):.*$", re.M | re.I)
_SAFE_EMAIL = re.compile(r"noreply|no-reply|example\.(?:com|org|net)|users\.noreply\.github\.com|localhost", re.I)
_PLACEHOLDER = re.compile(r"(?i)x{6,}|\*{4,}|changeme|example|dummy|fake|test|placeholder|your[_-]")


def entropy(s: str) -> float:
    counts = Counter(s)
    return -sum(c / len(s) * math.log2(c / len(s)) for c in counts.values()) if s else 0.0


@dataclass
class Finding:
    instance_id: str
    field: str
    kind: str          # a secret type, "high_entropy_secret", "email" or "trailer"
    severity: str      # secret | pii
    excerpt: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _mask(s: str) -> str:
    return s[:4] + "…" + s[-2:] if len(s) > 8 else "…"


FIELDS = ("problem_statement", "hints", "requirements", "interface", "gold_patch", "test_patch")
AGENT_VISIBLE = ("problem_statement", "hints", "requirements", "interface")


def scan_task(task: Task) -> list[Finding]:
    out: list[Finding] = []
    for field_name in FIELDS:
        text = getattr(task, field_name) or ""
        for kind, rx in SECRET_RES:
            for m in rx.finditer(text):
                out.append(Finding(task.instance_id, field_name, kind, "secret", _mask(m.group(0))))
        for m in _ASSIGN_RE.finditer(text):
            value = m.group(1)
            if entropy(value) >= 3.5 and not _PLACEHOLDER.search(value):
                out.append(Finding(task.instance_id, field_name, "high_entropy_secret", "secret", _mask(value)))
        for m in TRAILER_RE.finditer(text):
            out.append(Finding(task.instance_id, field_name, "trailer", "pii", m.group(0)[:60]))
        for m in EMAIL_RE.finditer(text):
            if not _SAFE_EMAIL.search(m.group(0)):
                out.append(Finding(task.instance_id, field_name, "email", "pii", _mask(m.group(0))))
    return out


def strip_trailers(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", TRAILER_RE.sub("", text)).strip()


def redact_task(task: Task) -> Task:
    """Remove trailers and email addresses from the agent-visible fields (patches are left intact)."""
    for field_name in AGENT_VISIBLE:
        text = strip_trailers(getattr(task, field_name) or "")
        text = EMAIL_RE.sub(lambda m: m.group(0) if _SAFE_EMAIL.search(m.group(0)) else "<email>", text)
        setattr(task, field_name, text)
    return task
