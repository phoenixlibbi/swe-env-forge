"""SWE-bench Pro style task descriptions: interface, requirements, and a leak check.

Pro adds two human-written fields so that strict unit tests do not reject valid
solutions: an **interface** (names, locations and signatures the tests call) and
**requirements** (behaviours the tests check, such as exact messages). This module
drafts both from the gold and test patches for a human to review, and checks that
agent-visible text does not leak the gold patch.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from .languages import definitions_in_line, language_of
from .patches import added_definitions, added_lines, identifiers, parse_patch
from .task import Task


# ------------------------------------------------------------------- interface
@dataclass
class InterfaceItem:
    kind: str          # Function | Method | Class | Type
    name: str
    location: str
    signature: str
    description: str = ""
    change: str = "new"   # new | changed

    def render(self) -> str:
        lines = [f"{self.kind}: {self.name}", f"Location: {self.location}", f"Signature: {self.signature}"]
        lines.append(f"Description: {self.description or 'TODO: describe what it does, its inputs and outputs.'}")
        return "\n".join(lines)


_KIND = {"function": "Function", "method": "Method", "class": "Class", "type": "Type", "const": "Function",
         "property-function": "Method"}


def _owners(task_patch: str) -> dict[tuple[str, str], str]:
    """(path, name) -> enclosing class for indentation-scoped languages (Python, Ruby)."""
    owners: dict[tuple[str, str], str] = {}
    for f in parse_patch(task_patch):
        lang = language_of(f.path)
        if lang not in ("python", "ruby"):
            continue
        for h in f.hunks:
            stack: list[tuple[int, str]] = []
            for raw in h.lines:
                if raw.startswith("-"):
                    continue
                line = raw[1:]
                if not line.strip():
                    continue
                indent = len(line) - len(line.lstrip())
                while stack and stack[-1][0] >= indent:
                    stack.pop()
                defs = definitions_in_line(line, lang)
                if not defs:
                    continue
                kind, name = defs[0]
                if stack and kind in ("function", "method"):
                    owners[(f.path, name)] = stack[-1][1]
                if kind in ("class", "type"):
                    stack.append((indent, name))
    return owners


def _docstring_after(path: str, signature: str, patch: str) -> str:
    for f in parse_patch(patch):
        if f.path != path:
            continue
        added = f.added
        for i, ln in enumerate(added):
            if ln.strip() == signature and i + 1 < len(added):
                m = re.match(r'\s*(?:"""|\'\'\'|///|//|#)\s*(.+?)\s*(?:"""|\'\'\')?\s*$', added[i + 1])
                if m:
                    return m.group(1)
    return ""


def draft_interface(task: Task) -> list[InterfaceItem]:
    """New or changed public definitions in the gold patch that the hidden tests reference."""
    used = identifiers(added_lines(task.test_patch))
    owners = _owners(task.gold_patch)
    items: list[InterfaceItem] = []
    for d in added_definitions(task.gold_patch):
        if d.name not in used or d.name.startswith("_") and not d.name.startswith("__"):
            continue
        f_is_test = any(f.path == d.path and f.is_test for f in parse_patch(task.gold_patch))
        if f_is_test:
            continue
        owner = owners.get((d.path, d.name))
        kind = "Method" if owner else _KIND.get(d.kind, "Function")
        items.append(InterfaceItem(kind=kind, name=f"{owner}.{d.name}" if owner else d.name, location=d.path,
                                   signature=d.signature.rstrip(":{ ").strip(),
                                   description=_docstring_after(d.path, d.signature, task.gold_patch),
                                   change="new" if d.is_new else "changed"))
    return items


def render_interface(items: list[InterfaceItem]) -> str:
    return "\n\n".join(i.render() for i in items)


# ---------------------------------------------------------------- requirements
_ASSERT_RE = re.compile(
    r"\bassert\b|\bexpect\s*\(|\bassert[A-Z_]\w*\s*\(|\bassert_\w+\s*\(|assert(?:_eq|_ne)?!\s*\(|\bt\.(?:Error|Fatal)f?\("
    r"|\brequire\.\w+\(|\bAssert\.\w+\(|\bshould\b|\.to(?:Be|Equal|Throw|Contain|Match)\w*\(|\bpytest\.raises\b")
_TEST_NAME_RE = re.compile(r"""(?:def\s+(test\w*)|func\s+(Test\w*)|\b(?:it|test)\s*\(\s*['"`]([^'"`]+)|\bfn\s+(\w+)\s*\(\)"""
                           r"""|void\s+(\w+)\s*\(|def\s+(test_\w+))""")
_STRING_RE = re.compile(r"""(?P<q>["'`])(?P<s>(?:\\.|(?!(?P=q)).){12,200})(?P=q)""")


@dataclass
class RequirementsDraft:
    checks: dict[str, list[str]] = field(default_factory=dict)   # test name -> assertion lines
    exact_strings: list[str] = field(default_factory=list)        # literals both the fix and the tests use

    def render(self) -> str:
        out = ["DRAFT: rewrite each bullet as expected behaviour. Never paste test code or the fix."]
        for s in self.exact_strings:
            out.append(f'- The solution must produce exactly the text "{s}" (the tests check it verbatim).')
        for name, lines in self.checks.items():
            for ln in lines[:4]:
                out.append(f"- [{name}] behaviour checked by: {ln}")
        return "\n".join(out)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def draft_requirements(task: Task) -> RequirementsDraft:
    draft = RequirementsDraft()
    current = "(module)"
    for line in added_lines(task.test_patch):
        m = _TEST_NAME_RE.search(line)
        if m:
            current = next(g for g in m.groups() if g)
        if _ASSERT_RE.search(line):
            draft.checks.setdefault(current, []).append(line.strip()[:200])
    gold_strings = {m.group("s") for ln in added_lines(task.gold_patch) for m in _STRING_RE.finditer(ln)}
    test_strings = {m.group("s") for ln in added_lines(task.test_patch) for m in _STRING_RE.finditer(ln)}
    draft.exact_strings = sorted(s for s in gold_strings & test_strings if " " in s or len(s) >= 20)
    return draft


# ------------------------------------------------------------------ leak check
_TOKEN_RE = re.compile(r"[A-Za-z_$][\w$]*|\d+|==|!=|<=|>=|->|=>|::|[^\s\w]")


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text)


@dataclass
class LeakReport:
    leaked: bool
    score: float                          # share of the text's n-grams found in the gold patch
    snippets: list[str] = field(default_factory=list)
    copied_lines: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def leak_check(text: str, gold_patch: str, n: int = 8, threshold: float = 0.02, allow: str = "") -> LeakReport:
    """Does agent-visible ``text`` copy code from the gold patch?

    Flags verbatim code lines (30+ characters) and shared token n-grams. Lines that
    appear in ``allow`` (typically the interface, whose signatures are meant to be
    shown) are ignored.
    """
    allowed = {ln.strip() for ln in allow.splitlines() if ln.strip()}
    code = [ln for ln in added_lines(gold_patch)
            if ln.strip() and not any(ln.strip() in a or a.endswith(ln.strip().rstrip(":{ ")) for a in allowed)]
    copied = sorted({ln.strip() for ln in code if len(ln.strip()) >= 30 and ln.strip() in text})
    gold_toks = _tokens("\n".join(code))
    gold_grams = {tuple(gold_toks[i:i + n]) for i in range(len(gold_toks) - n + 1)}
    text_clean = "\n".join(ln for ln in text.splitlines() if ln.strip() not in allowed)
    toks = _tokens(text_clean)
    grams = [tuple(toks[i:i + n]) for i in range(len(toks) - n + 1)]
    hits = [g for g in grams if g in gold_grams]
    score = len(hits) / len(grams) if grams else 0.0
    snippets = []
    for g in hits[:10]:
        s = " ".join(g)
        if s not in snippets:
            snippets.append(s)
    return LeakReport(leaked=bool(copied) or score > threshold, score=round(score, 4), snippets=snippets,
                      copied_lines=copied)


# ------------------------------------------------------------------ augment
PROBLEM_PROMPT = """You write GitHub-issue style problem statements for software-engineering benchmark tasks.
Write the issue a user or maintainer would file BEFORE the change existed: the motivation, the current
behaviour, the expected behaviour and, if useful, steps to reproduce. Do not describe the implementation,
do not mention file names or functions unless a user would know them, never include code from the fix,
and do not mention tests. Reply with the issue text only (start with a one-line title)."""


def llm_problem_statement(task: Task, client) -> str:
    msg = (f"Repository: {task.repo}\n\nOriginal commit message / issue:\n{task.problem_statement}\n\n"
           f"The change that resolved it (for your understanding only, do not leak it):\n{task.gold_patch[:12000]}\n\n"
           f"Tests that were added (do not mention them):\n{task.test_patch[:6000]}")
    text, _usage = client.chat([{"role": "system", "content": PROBLEM_PROMPT}, {"role": "user", "content": msg}])
    return text.strip()


def augment(task: Task, client=None, overwrite: bool = False) -> tuple[Task, dict[str, Any]]:
    """Fill ``interface`` and ``requirements`` drafts (and, with an LLM, the problem statement)."""
    report: dict[str, Any] = {}
    items = draft_interface(task)
    if items and (overwrite or not task.interface):
        task.interface = render_interface(items)
    report["interface_items"] = [asdict(i) for i in items]
    req = draft_requirements(task)
    if (req.checks or req.exact_strings) and (overwrite or not task.requirements):
        task.requirements = req.render()
    report["requirements"] = req.to_dict()
    if client is not None:
        task.problem_statement = llm_problem_statement(task, client)
        task.metadata["needs_augmentation"] = False
    report["leak"] = {
        "problem_statement": leak_check(task.problem_statement, task.gold_patch, allow=task.interface).to_dict(),
        "requirements": leak_check(task.requirements, task.gold_patch, allow=task.interface).to_dict(),
    }
    task.metadata["spec"] = {"drafted": True, "reviewed": False,
                             "leak": any(v["leaked"] for v in report["leak"].values())}
    return task, report
