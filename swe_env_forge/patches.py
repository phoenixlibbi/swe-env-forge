"""Unified-diff parsing: split a fix into test / source parts, measure it, list new definitions."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Iterable

from .languages import definitions_in_line, is_code_path, is_test_path, language_of

_DIFF_GIT = re.compile(r"^diff --git (?P<a>\"?a/.*?\"?) (?P<b>\"?b/.*\"?)$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    header: str
    old_start: int
    new_start: int
    lines: list[str] = field(default_factory=list)


@dataclass
class FilePatch:
    path: str                 # path after the change (old path for deletions)
    old_path: str
    text: str                 # the raw diff chunk for this file
    added_file: bool = False
    deleted: bool = False
    renamed: bool = False
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added(self) -> list[str]:
        return [ln[1:] for h in self.hunks for ln in h.lines if ln.startswith("+")]

    @property
    def removed(self) -> list[str]:
        return [ln[1:] for h in self.hunks for ln in h.lines if ln.startswith("-")]

    @property
    def is_test(self) -> bool:
        return is_test_path(self.path) or (self.renamed and is_test_path(self.old_path))


_ESCAPES = {"n": b"\n", "t": b"\t", '"': b'"', "\\": b"\\", "a": b"\a", "b": b"\b", "f": b"\f", "r": b"\r", "v": b"\v"}


def _unquote(p: str) -> str:
    """Undo git's C-style path quoting (``"t\\303\\251st.py"`` -> ``tést.py``), byte-wise."""
    if not (len(p) >= 2 and p[0] == p[-1] == '"'):
        return p
    body, out, i = p[1:-1], bytearray(), 0
    while i < len(body):
        c = body[i]
        if c == "\\" and i + 1 < len(body):
            nxt = body[i + 1]
            if nxt in "01234567" and re.match(r"[0-7]{3}", body[i + 1:i + 4]):
                out.append(int(body[i + 1:i + 4], 8))
                i += 4
                continue
            out += _ESCAPES.get(nxt, nxt.encode("utf-8"))
            i += 2
            continue
        out += c.encode("utf-8")
        i += 1
    return out.decode("utf-8", "replace")


def _strip_prefix(p: str) -> str:
    p = _unquote(p.strip())
    if p == "/dev/null":
        return p
    return p[2:] if p[:2] in ("a/", "b/") else p


def _split_chunks(text: str) -> list[list[str]]:
    """Split a multi-file diff into per-file line chunks."""
    lines = text.splitlines(keepends=True)
    chunks: list[list[str]] = []
    has_git_headers = any(ln.startswith("diff --git ") for ln in lines)
    current: list[str] | None = None
    i = 0
    while i < len(lines):
        ln = lines[i]
        # without git headers a file starts with ---/+++ followed by a hunk header; requiring the
        # @@ line stops "-- x" / "++ y" content lines inside a hunk from looking like a new file
        starts = ln.startswith("diff --git ") if has_git_headers else (
            ln.startswith("--- ") and i + 2 < len(lines) and lines[i + 1].startswith("+++ ")
            and lines[i + 2].startswith("@@"))
        if starts:
            current = [ln]
            chunks.append(current)
        elif current is not None:
            current.append(ln)
        i += 1
    return chunks


def parse_patch(text: str | None) -> list[FilePatch]:
    """Parse a unified diff (``git diff`` output or plain ``diff -u``) into per-file patches."""
    if not text or not text.strip():
        return []
    files: list[FilePatch] = []
    for chunk in _split_chunks(text):
        head = chunk[0].rstrip("\n")
        old_path = new_path = ""
        m = _DIFF_GIT.match(head)
        if m:
            old_path, new_path = _strip_prefix(m.group("a")), _strip_prefix(m.group("b"))
        fp = FilePatch(path=new_path, old_path=old_path, text="".join(chunk))
        hunk: Hunk | None = None
        for raw in chunk[1:] if m else chunk:
            ln = raw.rstrip("\n").rstrip("\r")
            if hunk is None or not (ln[:1] in (" ", "+", "-", "\\") or ln == ""):
                if ln.startswith("--- "):
                    old_path = _strip_prefix(ln[4:].split("\t")[0])
                    continue
                if ln.startswith("+++ "):
                    new_path = _strip_prefix(ln[4:].split("\t")[0])
                    continue
                if ln.startswith("new file mode"):
                    fp.added_file = True
                elif ln.startswith("deleted file mode"):
                    fp.deleted = True
                elif ln.startswith("rename from "):
                    fp.renamed, old_path = True, _unquote(ln[len("rename from "):])
                elif ln.startswith("rename to "):
                    fp.renamed, new_path = True, _unquote(ln[len("rename to "):])
                elif ln.startswith("Binary files ") or ln.startswith("GIT binary patch"):
                    fp.binary = True
            hm = _HUNK.match(ln)
            if hm:
                hunk = Hunk(header=ln, old_start=int(hm.group(1)), new_start=int(hm.group(3)))
                fp.hunks.append(hunk)
                continue
            if hunk is not None and (ln[:1] in (" ", "+", "-", "\\") or ln == ""):
                if ln.startswith("\\"):
                    continue
                hunk.lines.append(ln if ln else " ")
        if old_path == "/dev/null":
            fp.added_file = True
        if new_path == "/dev/null":
            fp.deleted = True
        fp.old_path = old_path if old_path != "/dev/null" else new_path
        fp.path = new_path if new_path and new_path != "/dev/null" else fp.old_path
        if fp.path:
            files.append(fp)
    return files


def join_patch(files: Iterable[FilePatch]) -> str:
    out = "".join(f.text if f.text.endswith("\n") else f.text + "\n" for f in files)
    return out


def split_patch(text: str, is_test: Callable[[FilePatch], bool] | None = None) -> tuple[str, str]:
    """Split a diff into (source_patch, test_patch) the way SWE-bench and Pro do."""
    is_test = is_test or (lambda f: f.is_test)
    files = parse_patch(text)
    return join_patch(f for f in files if not is_test(f)), join_patch(f for f in files if is_test(f))


def strip_binary(text: str) -> str:
    """Drop binary file sections (``git apply`` cannot apply them without full index)."""
    return join_patch(f for f in parse_patch(text) if not f.binary)


def files_in(text: str) -> list[str]:
    return [f.path for f in parse_patch(text)]


@dataclass
class PatchStats:
    files: int = 0
    code_files: int = 0
    test_files: int = 0
    hunks: int = 0
    lines_added: int = 0
    lines_removed: int = 0

    @property
    def lines_changed(self) -> int:
        return self.lines_added + self.lines_removed

    @property
    def lines_edited(self) -> int:
        """Lines a human would say they touched: a 1-line replacement is 1, not 2."""
        return max(self.lines_added, self.lines_removed)

    def to_dict(self) -> dict[str, int]:
        return {"files": self.files, "code_files": self.code_files, "test_files": self.test_files,
                "hunks": self.hunks, "lines_added": self.lines_added, "lines_removed": self.lines_removed,
                "lines_changed": self.lines_changed, "lines_edited": self.lines_edited}


def _meaningful(line: str) -> bool:
    return bool(line.strip())


def patch_stats(text: str, ignore_blank: bool = True) -> PatchStats:
    s = PatchStats()
    for f in parse_patch(text):
        s.files += 1
        s.code_files += int(is_code_path(f.path))
        s.test_files += int(f.is_test)
        s.hunks += len(f.hunks)
        keep = _meaningful if ignore_blank else (lambda _l: True)
        s.lines_added += sum(1 for ln in f.added if keep(ln))
        s.lines_removed += sum(1 for ln in f.removed if keep(ln))
    return s


@dataclass(frozen=True)
class Definition:
    name: str
    kind: str
    path: str
    signature: str
    language: str
    is_new: bool          # not defined in the removed lines of the same file


def added_definitions(text: str) -> list[Definition]:
    """Functions / classes / types whose definition line appears in the added lines."""
    out: list[Definition] = []
    for f in parse_patch(text):
        lang = language_of(f.path)
        if not lang or f.binary:
            continue
        removed = {name for ln in f.removed for _k, name in definitions_in_line(ln, lang)}
        seen = set()
        for ln in f.added:
            for kind, name in definitions_in_line(ln, lang):
                if name in seen:
                    continue
                seen.add(name)
                out.append(Definition(name=name, kind=kind, path=f.path, signature=ln.strip(),
                                      language=lang, is_new=name not in removed))
    return out


_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def identifiers(lines: Iterable[str]) -> set[str]:
    return {m.group(0) for ln in lines for m in _IDENT.finditer(ln)}


def added_lines(text: str, only_tests: bool | None = None) -> list[str]:
    """Added lines of a diff, optionally restricted to test (True) or non-test (False) files."""
    out = []
    for f in parse_patch(text):
        if only_tests is None or f.is_test == only_tests:
            out.extend(f.added)
    return out
