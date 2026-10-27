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
# Pro's last step was human: experts rewrote each problem statement and wrote the requirements and
# interface descriptions. Three ways to do that step here:
#   * human  - spec-export writes one Markdown file per task, people edit it, spec-import reads it back
#   * llm    - LLMWriter asks a model (any OpenAI-compatible endpoint) to write the fields
#   * custom - any object with ``write(task, drafts) -> dict`` (``--writer module.path:Class``)
# Every path runs the same leak check, and records who wrote the text and whether a human reviewed it.

WRITER_PROMPT = """You prepare software-engineering benchmark tasks the way SWE-bench Pro's annotators did.
You receive a raw task: an original issue or commit message, draft interface items and draft requirements
extracted automatically, plus the reference fix and the hidden tests for your understanding only.

Write three things for the engineer who will solve the task WITHOUT seeing the fix or the tests:
1. problem_statement: an issue as a user or maintainer would file it before the change existed. Title on the
   first line, then motivation, current behaviour, expected behaviour, and steps to reproduce if useful.
2. requirements: a list of precise, testable behaviours the solution must have. Include exact strings,
   names, return values or error types when the tests check them verbatim. Describe WHAT, never HOW.
3. interface_descriptions: for each interface item, one sentence on what it does, its inputs and its output.
   Keep names, locations and signatures exactly as given.

Rules: never copy code from the reference fix, never describe the implementation, never mention the tests.
Reply with one JSON object and nothing else:
{"problem_statement": "...", "requirements": ["...", "..."], "interface_descriptions": {"<name>": "..."}}"""


def _json_object(text: str) -> dict[str, Any] | None:
    import json

    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    start = text.find("{")
    if start != -1:
        candidates.append(text[start:text.rfind("}") + 1])
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except ValueError:
            continue
    return None


class LLMWriter:
    """Writes the human-augmentation fields with a chat model, retrying when the leak check fires."""

    def __init__(self, client, max_retries: int = 1, max_patch_chars: int = 16_000):
        self.client, self.max_retries, self.max_patch_chars = client, max_retries, max_patch_chars
        self.name = f"llm:{getattr(client, 'model', '') or 'model'}"

    def write(self, task: Task, drafts: dict[str, Any]) -> dict[str, Any]:
        import json

        items = drafts.get("interface_items", [])
        msg = (f"Repository: {task.repo}\n\nOriginal issue / commit message:\n{drafts['original_statement']}\n\n"
               f"Draft interface items:\n{json.dumps(items, indent=1)}\n\n"
               f"Draft requirements (from the tests; rewrite them as behaviour):\n{drafts['requirements_draft']}\n\n"
               f"Reference fix (do not leak):\n{task.gold_patch[:self.max_patch_chars]}\n\n"
               f"Hidden tests (do not mention):\n{task.test_patch[:self.max_patch_chars // 2]}")
        messages = [{"role": "system", "content": WRITER_PROMPT}, {"role": "user", "content": msg}]
        out: dict[str, Any] = {}
        for attempt in range(self.max_retries + 1):
            text, _usage = self.client.chat(messages)
            obj = _json_object(text)
            if obj is None:  # a model that ignores the format still gives us a problem statement
                out = {"problem_statement": text.strip()}
            else:
                reqs = obj.get("requirements") or []
                if isinstance(reqs, str):
                    reqs = [ln.lstrip("-* ").strip() for ln in reqs.splitlines() if ln.strip()]
                out = {"problem_statement": str(obj.get("problem_statement", "")).strip(),
                       "requirements": "\n".join(f"- {r.strip()}" for r in reqs if str(r).strip()),
                       "interface_descriptions": obj.get("interface_descriptions") or {}}
            out["attempts"] = attempt + 1
            leaks = [leak_check(out.get(k, ""), task.gold_patch) for k in ("problem_statement", "requirements")]
            snippets = [sn for lk in leaks if lk.leaked for sn in (lk.copied_lines or lk.snippets)]
            if not snippets or attempt == self.max_retries:
                return out
            messages += [{"role": "assistant", "content": text},
                         {"role": "user", "content": "Your text copies code from the reference fix:\n"
                          + "\n".join(f"- {sn}" for sn in snippets[:10])
                          + "\nRewrite it in plain language without these snippets. Same JSON format."}]
        return out


def drafts_for(task: Task) -> dict[str, Any]:
    items = draft_interface(task)
    req = draft_requirements(task)
    return {"original_statement": task.problem_statement, "interface_items": [asdict(i) for i in items],
            "requirements_draft": req.render() if (req.checks or req.exact_strings) else "", "_items": items,
            "_requirements": req}


def augment(task: Task, client=None, overwrite: bool = False, writer=None) -> tuple[Task, dict[str, Any]]:
    """Fill the Pro-style fields of ``task``.

    Without a writer: deterministic drafts (interface from the code, requirements from the
    tests). With ``writer`` (or ``client``, which is wrapped in :class:`LLMWriter`): the writer
    produces the final problem statement, requirements and interface descriptions.
    """
    if writer is None and client is not None:
        writer = LLMWriter(client)
    d = drafts_for(task)
    items: list[InterfaceItem] = d.pop("_items")
    req: RequirementsDraft = d.pop("_requirements")
    report: dict[str, Any] = {"interface_items": d["interface_items"], "requirements": req.to_dict()}
    fields_by: dict[str, str] = {}
    if writer is None:
        if items and (overwrite or not task.interface):
            task.interface, fields_by["interface"] = render_interface(items), "draft"
        if d["requirements_draft"] and (overwrite or not task.requirements):
            task.requirements, fields_by["requirements"] = d["requirements_draft"], "draft"
        mode = "draft"
    else:
        out = writer.write(task, d) or {}
        name = getattr(writer, "name", type(writer).__name__)
        descs = out.get("interface_descriptions") or {}
        for it in items:
            desc = descs.get(it.name) or descs.get(it.name.split(".")[-1])
            if desc and not it.description:
                it.description = str(desc).strip()
        if items and (overwrite or not task.interface):
            task.interface, fields_by["interface"] = render_interface(items), name
        if out.get("interface") and (overwrite or not task.interface):
            task.interface, fields_by["interface"] = str(out["interface"]), name
        if out.get("requirements") and (overwrite or not task.requirements or task.requirements.startswith("DRAFT")):
            task.requirements, fields_by["requirements"] = str(out["requirements"]), name
        if out.get("problem_statement"):
            task.problem_statement, fields_by["problem_statement"] = str(out["problem_statement"]), name
            task.metadata["needs_augmentation"] = False
        report["writer"] = {"name": name, "attempts": out.get("attempts", 1)}
        mode = "llm" if isinstance(writer, LLMWriter) else "custom"
    report["leak"] = {
        "problem_statement": leak_check(task.problem_statement, task.gold_patch, allow=task.interface).to_dict(),
        "requirements": leak_check(task.requirements, task.gold_patch, allow=task.interface).to_dict(),
    }
    task.metadata["spec"] = {"mode": mode, "fields_by": fields_by, "reviewed": False, "reviewer": "",
                             "leak": any(v["leaked"] for v in report["leak"].values())}
    return task, report


# ---------------------------------------------------------- human review files
_FIELDS = (("problem_statement", "Problem statement"), ("requirements", "Requirements"), ("interface", "Interface"))
_FIELD_RE = re.compile(r"<!-- field: (\w+) -->\n(.*?)\n<!-- end field -->", re.S)
_META_RE = re.compile(r"<!-- meta\n(.*?)\n-->", re.S)


def export_review_file(task: Task) -> str:
    """One Markdown file a person can edit: the three agent-visible fields, then read-only context."""
    spec = task.metadata.get("spec", {})
    meta = "\n".join([f"instance_id: {task.instance_id}", "status: draft    # set to: reviewed",
                      "reviewer:        # your name", f"written_by: {spec.get('mode', 'original')}"])
    parts = [f"# Review: {task.instance_id}", "",
             "Edit only the text between the field markers. Agents will see exactly these three fields.",
             "Do not copy code from the reference fix and do not mention the tests.", "",
             f"<!-- meta\n{meta}\n-->", ""]
    for key, title in _FIELDS:
        parts += [f"## {title}", "", f"<!-- field: {key} -->", getattr(task, key).strip(), "<!-- end field -->", ""]
    parts += ["---", "## Context for the reviewer (ignored on import, hidden from agents)", "",
              "### Reference fix", "", "```diff", task.gold_patch.rstrip(), "```", "",
              "### Hidden tests", "", "```diff", task.test_patch.rstrip(), "```", ""]
    if task.fail_to_pass:
        parts += ["### FAIL_TO_PASS", "", *[f"- `{t}`" for t in task.fail_to_pass], ""]
    return "\n".join(parts)


def import_review_file(task: Task, text: str) -> dict[str, Any]:
    """Apply an edited review file to ``task``; returns what changed and the leak report."""
    fields = {k: v.strip() for k, v in _FIELD_RE.findall(text)}
    meta: dict[str, str] = {}
    m = _META_RE.search(text)
    if m:
        for line in m.group(1).splitlines():
            key, _, value = line.partition(":")
            meta[key.strip()] = value.split("#", 1)[0].strip()
    if meta.get("instance_id") and meta["instance_id"] != task.instance_id:
        raise ValueError(f"review file is for {meta['instance_id']}, not {task.instance_id}")
    changed = [k for k, _t in _FIELDS if k in fields and fields[k] != getattr(task, k).strip()]
    for k in changed:
        setattr(task, k, fields[k])
    reviewed = meta.get("status", "").lower() == "reviewed"
    leak = {k: leak_check(getattr(task, k), task.gold_patch, allow=task.interface).to_dict()
            for k in ("problem_statement", "requirements")}
    spec = task.metadata.setdefault("spec", {})
    spec.update({"reviewed": reviewed, "reviewer": meta.get("reviewer", ""),
                 "leak": any(v["leaked"] for v in leak.values())})
    fb = spec.setdefault("fields_by", {})
    for k in changed:
        fb[k] = f"human:{meta.get('reviewer') or 'unknown'}"
    if reviewed:
        task.metadata["needs_augmentation"] = False
    return {"instance_id": task.instance_id, "changed": changed, "reviewed": reviewed,
            "reviewer": meta.get("reviewer", ""), "leak": leak}
