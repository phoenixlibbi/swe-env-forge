"""SWE-agent style tools for the gym: a file editor, search and find-file.

SWE-bench Pro evaluated agents in SWE-agent, whose agent-computer interface gives the
model purpose-built tools instead of raw shell editing: ``str_replace_editor`` (view with
line numbers, create, str_replace, insert, undo_edit), plus repository search. These are
the same tools, run by the harness through the sandbox's file API, so they work in any
image and any language with nothing installed inside the container.

Actions (all paths are relative to the repository root, or absolute inside it)::

    {"type": "editor", "command": "view", "path": "pkg/a.py", "view_range": [10, 40]}
    {"type": "editor", "command": "create", "path": "pkg/new.py", "file_text": "..."}
    {"type": "editor", "command": "str_replace", "path": "pkg/a.py", "old_str": "...", "new_str": "..."}
    {"type": "editor", "command": "insert", "path": "pkg/a.py", "insert_line": 12, "new_str": "..."}
    {"type": "editor", "command": "undo_edit", "path": "pkg/a.py"}
    {"type": "search", "pattern": "def apply_discount", "path": ".", "regex": false}
    {"type": "find_file", "name": "*.py", "path": "pkg"}
"""

from __future__ import annotations

import posixpath
import shlex
from typing import Any

from .sandbox import Sandbox, SandboxError

SNIPPET = 4          # context lines shown around an edit
MAX_HITS = 100


def _numbered(lines: list[str], start: int = 1) -> str:
    return "\n".join(f"{i:6}\t{ln}" for i, ln in enumerate(lines, start))


class Toolkit:
    def __init__(self, sb: Sandbox):
        self.sb = sb
        self.root = sb.workdir.rstrip("/")
        self.history: dict[str, list[str]] = {}

    # ------------------------------------------------------------------ paths
    def _path(self, path: Any) -> str:
        p = str(path or ".").strip()
        full = posixpath.normpath(p if p.startswith("/") else f"{self.root}/{p}")
        if full != self.root and not full.startswith(self.root + "/"):
            raise ValueError(f"{p}: the editor only works inside the repository ({self.root})")
        return full

    def _rel(self, full: str) -> str:
        return posixpath.relpath(full, self.root)

    def _is_dir(self, full: str) -> bool:
        return self.sb.exec(f"test -d {shlex.quote(full)}", timeout=30).ok

    def _exists(self, full: str) -> bool:
        return self.sb.exec(f"test -e {shlex.quote(full)}", timeout=30).ok

    def _read(self, full: str) -> str:
        return self.sb.read_file(full)

    def _write(self, full: str, text: str) -> None:
        self.sb.write_file(full, text)

    def _snippet(self, text: str, first_line: int, n_lines: int) -> str:
        lines = text.split("\n")
        lo = max(0, first_line - 1 - SNIPPET)
        hi = min(len(lines), first_line - 1 + n_lines + SNIPPET)
        return _numbered(lines[lo:hi], lo + 1)

    # --------------------------------------------------------------- dispatch
    def run(self, action: dict[str, Any]) -> tuple[str, int]:
        """Execute one tool action; returns (output, exit_code). Errors become messages, not exceptions."""
        try:
            kind = action.get("type")
            if kind == "search":
                return self.search(action.get("pattern", ""), action.get("path", "."), bool(action.get("regex")))
            if kind == "find_file":
                return self.find_file(action.get("name", ""), action.get("path", "."))
            cmd = action.get("command")
            handler = {"view": self.view, "create": self.create, "str_replace": self.str_replace,
                       "insert": self.insert, "undo_edit": self.undo_edit}.get(cmd)
            if handler is None:
                return f"unknown editor command {cmd!r}; use view, create, str_replace, insert or undo_edit", 2
            return handler(action)
        except (ValueError, SandboxError) as exc:
            return f"error: {exc}", 1

    # ----------------------------------------------------------------- editor
    def view(self, a: dict[str, Any]) -> tuple[str, int]:
        full = self._path(a.get("path"))
        if self._is_dir(full):
            res = self.sb.exec(f"find {shlex.quote(full)} -maxdepth 2 -not -path '*/.*' | sort | head -n 500", timeout=60)
            listing = "\n".join(self._rel(p) if p.startswith(self.root) else p for p in res.output.split())
            return f"Files and directories up to 2 levels deep in {self._rel(full)}:\n{listing}", 0
        if not self._exists(full):
            return f"error: {self._rel(full)} does not exist", 1
        lines = self._read(full).split("\n")
        rng = a.get("view_range")
        start, end = 1, len(lines)
        if rng:
            start = max(1, int(rng[0]))
            end = len(lines) if int(rng[1]) == -1 else min(len(lines), int(rng[1]))
            if start > end:
                return f"error: invalid view_range {rng} for a file with {len(lines)} lines", 1
        return f"Here's the result of running `cat -n` on {self._rel(full)}:\n{_numbered(lines[start - 1:end], start)}", 0

    def create(self, a: dict[str, Any]) -> tuple[str, int]:
        full = self._path(a.get("path"))
        if self._exists(full):
            return f"error: {self._rel(full)} already exists; use str_replace or insert to change it", 1
        self._write(full, str(a.get("file_text", "")))
        self.history.setdefault(full, []).append(None)  # type: ignore[arg-type]  # undo deletes it
        return f"File created successfully at: {self._rel(full)}", 0

    def str_replace(self, a: dict[str, Any]) -> tuple[str, int]:
        full = self._path(a.get("path"))
        old, new = str(a.get("old_str", "")), str(a.get("new_str", ""))
        if not old:
            return "error: old_str must not be empty", 1
        text = self._read(full)
        count = text.count(old)
        if count == 0:
            return (f"error: old_str did not appear verbatim in {self._rel(full)}. Check whitespace and "
                    "indentation (view the file first)."), 1
        if count > 1:
            lines = [i + 1 for i, ln in enumerate(text.split("\n")) if old.split("\n")[0] in ln]
            return (f"error: old_str occurs {count} times in {self._rel(full)} (lines {lines[:20]}). "
                    "Include more context to make it unique."), 1
        first_line = text[:text.index(old)].count("\n") + 1
        updated = text.replace(old, new, 1)
        self.history.setdefault(full, []).append(text)
        self._write(full, updated)
        return (f"The file {self._rel(full)} has been edited. A snippet of the result:\n"
                f"{self._snippet(updated, first_line, new.count(chr(10)) + 1)}\nReview the change and edit again if needed."), 0

    def insert(self, a: dict[str, Any]) -> tuple[str, int]:
        full = self._path(a.get("path"))
        text = self._read(full)
        lines = text.split("\n")
        try:
            at = int(a.get("insert_line"))
        except (TypeError, ValueError):
            return "error: insert_line must be an integer", 1
        if not 0 <= at <= len(lines):
            return f"error: insert_line must be between 0 and {len(lines)}", 1
        new_lines = str(a.get("new_str", "")).split("\n")
        updated = "\n".join(lines[:at] + new_lines + lines[at:])
        self.history.setdefault(full, []).append(text)
        self._write(full, updated)
        return (f"The file {self._rel(full)} has been edited. A snippet of the result:\n"
                f"{self._snippet(updated, at + 1, len(new_lines))}"), 0

    def undo_edit(self, a: dict[str, Any]) -> tuple[str, int]:
        full = self._path(a.get("path"))
        stack = self.history.get(full)
        if not stack:
            return f"error: no edit history for {self._rel(full)}", 1
        previous = stack.pop()
        if previous is None:
            self.sb.exec(f"rm -f -- {shlex.quote(full)}", timeout=30)
            return f"Removed {self._rel(full)} (undid its creation)", 0
        self._write(full, previous)
        return f"Last edit to {self._rel(full)} undone.", 0

    # ----------------------------------------------------------------- search
    def search(self, pattern: str, path: str = ".", regex: bool = False) -> tuple[str, int]:
        if not pattern:
            return "error: pattern must not be empty", 1
        full = self._path(path)
        flag = "-E" if regex else "-F"
        res = self.sb.exec(f"grep -rnI {flag} --exclude-dir=.git -e {shlex.quote(pattern)} {shlex.quote(full)} "
                           f"| head -n {MAX_HITS + 1}", timeout=120)
        hits = [ln.replace(self.root + "/", "", 1) for ln in res.output.splitlines() if ln.strip()]
        if not hits:
            return f"No matches for {pattern!r} in {self._rel(full)}", 0
        more = len(hits) > MAX_HITS
        body = "\n".join(hits[:MAX_HITS])
        return (f"Matches for {pattern!r} in {self._rel(full)}"
                f"{' (first ' + str(MAX_HITS) + ', narrow the search)' if more else ''}:\n{body}"), 0

    def find_file(self, name: str, path: str = ".") -> tuple[str, int]:
        if not name:
            return "error: name must not be empty", 1
        full = self._path(path)
        res = self.sb.exec(f"find {shlex.quote(full)} -name {shlex.quote(name)} -not -path '*/.git/*' "
                           f"| sort | head -n {MAX_HITS + 1}", timeout=120)
        found = [self._rel(p) for p in res.output.split() if p.startswith(self.root)]
        if not found:
            return f"No files named {name!r} under {self._rel(full)}", 0
        return f"Found {len(found[:MAX_HITS])} file(s):\n" + "\n".join(found[:MAX_HITS]), 0


# OpenAI-style function schemas for the tools above (plus bash and submit).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "bash", "description": "Run one shell command in the repository (cwd persists between calls). "
        "Output is truncated when long; prefer the editor and search tools for reading code.",
        "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "str_replace_editor",
        "description": "View, create and edit files. view shows a file with line numbers (or a directory two "
        "levels deep); str_replace replaces one exact, unique occurrence of old_str; insert adds new_str after "
        "insert_line; create writes a new file; undo_edit reverts the last edit of a file.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "enum": ["view", "create", "str_replace", "insert", "undo_edit"]},
            "path": {"type": "string", "description": "path relative to the repository root"},
            "file_text": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"},
            "insert_line": {"type": "integer"},
            "view_range": {"type": "array", "items": {"type": "integer"}, "description": "[start, end], end -1 = EOF"}},
            "required": ["command", "path"]}}},
    {"type": "function", "function": {
        "name": "search", "description": "Search file contents (grep). Literal text unless regex is true.",
        "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}, "path": {"type": "string"},
                                                        "regex": {"type": "boolean"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {
        "name": "find_file", "description": "Find files by name or glob, e.g. '*.py' or 'setup.cfg'.",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}, "path": {"type": "string"}},
                       "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "submit", "description": "Finish: your changes to the repository are your answer.",
        "parameters": {"type": "object", "properties": {}}}},
]


def action_from_tool_call(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Map a function call from the model to a gym action."""
    if name == "bash":
        return {"type": "bash", "command": str(args.get("command", ""))}
    if name == "str_replace_editor":
        return {"type": "editor", **args}
    if name in ("search", "find_file"):
        return {"type": name, **args}
    if name == "submit":
        return {"type": "submit"}
    return {"type": "unknown_tool", "name": name}
