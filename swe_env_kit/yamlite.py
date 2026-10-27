"""A small YAML reader for CI and compose files (standard library only).

Uses PyYAML when it is installed. Otherwise it parses the subset that GitHub Actions
workflows and docker-compose files use in practice: block mappings and sequences, flow
collections (``[a, b]``, ``{k: v}``), quoted and plain scalars, block scalars (``|``, ``>``)
and comments. Anchors, aliases, tags and multi-document streams are not supported.
"""

from __future__ import annotations

import re
from typing import Any


class YAMLError(ValueError):
    pass


def loads(text: str) -> Any:
    try:
        import yaml  # type: ignore

        return yaml.safe_load(text)
    except ImportError:
        return _Parser(text).parse()


def _strip_comment(line: str) -> str:
    out, quote = [], None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(tok: str) -> Any:
    t = tok.strip()
    if not t:
        return None
    if t[0] in "[{":
        return _flow(t)
    if len(t) >= 2 and t[0] == t[-1] and t[0] in "'\"":
        body = t[1:-1]
        return body.replace("''", "'") if t[0] == "'" else body.encode().decode("unicode_escape")
    low = t.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "~"):
        return None
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if re.fullmatch(r"-?\d+\.\d+", t):
        return float(t)
    return t


def _split_flow(body: str) -> list[str]:
    parts, depth, quote, cur = [], 0, None, []
    for ch in body:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    if "".join(cur).strip():
        parts.append("".join(cur))
    return parts


def _flow(t: str) -> Any:
    t = t.strip()
    if t.startswith("[") and t.endswith("]"):
        return [_scalar(p) for p in _split_flow(t[1:-1])]
    if t.startswith("{") and t.endswith("}"):
        out = {}
        for p in _split_flow(t[1:-1]):
            k, _, v = p.partition(":")
            out[str(_scalar(k))] = _scalar(v)
        return out
    return t


_KEY = re.compile(r"""^(?P<key>"[^"]*"|'[^']*'|[^:#'"{}\[\]][^:#]*?)\s*:(?:\s+(?P<val>.*)|\s*$)""")


class _Parser:
    def __init__(self, text: str):
        self.lines: list[tuple[int, str]] = []
        raw = text.replace("\t", "  ").splitlines()
        self.raw = raw
        self.i = 0

    def parse(self) -> Any:
        self.i = 0
        return self._block(self._next_indent())

    # -------------------------------------------------------------- helpers
    def _content(self, idx: int) -> str | None:
        line = _strip_comment(self.raw[idx])
        return line if line.strip() and line.strip() not in ("---", "...") else None

    def _next_indent(self) -> int:
        while self.i < len(self.raw):
            c = self._content(self.i)
            if c is not None:
                return len(c) - len(c.lstrip())
            self.i += 1
        return -1

    def _block_scalar(self, indicator: str, parent_indent: int) -> str:
        lines, ind = [], None
        while self.i < len(self.raw):
            raw = self.raw[self.i]
            if raw.strip() == "":
                lines.append("")
                self.i += 1
                continue
            cur = len(raw) - len(raw.lstrip())
            if cur <= parent_indent:
                break
            ind = cur if ind is None else ind
            lines.append(raw[ind:] if len(raw) >= ind else raw.lstrip())
            self.i += 1
        while lines and lines[-1] == "":
            lines.pop()
        if indicator.startswith(">"):
            return " ".join(ln.strip() for ln in lines if ln.strip()) + "\n"
        return "\n".join(lines) + ("\n" if not indicator.endswith("-") else "")

    def _value(self, val: str | None, indent: int) -> Any:
        if val is None or val == "":
            nxt = self._next_indent()
            if nxt > indent:
                return self._block(nxt)
            # a sequence may sit at the same indent as its parent key
            if nxt == indent and nxt >= 0 and (self._content(self.i) or "").lstrip().startswith("- "):
                return self._block(nxt)
            return None
        if val[:1] in "|>":
            return self._block_scalar(val, indent)
        return _scalar(val)

    # ---------------------------------------------------------------- blocks
    def _block(self, indent: int) -> Any:
        if indent < 0:
            return None
        first = self._content(self.i)
        if first is None:
            return None
        if first.lstrip().startswith("- ") or first.strip() == "-":
            return self._seq(indent)
        return self._map(indent)

    def _seq(self, indent: int) -> list:
        out = []
        while True:
            cur = self._next_indent()
            if cur != indent:
                break
            line = self._content(self.i)
            stripped = line.lstrip()
            if not (stripped.startswith("- ") or stripped == "-"):
                break
            item = stripped[2:] if stripped.startswith("- ") else ""
            self.i += 1
            if not item.strip():
                out.append(self._value(None, indent))
                continue
            m = _KEY.match(item)
            if m and not item.lstrip().startswith(("[", "{")):
                # "- key: value" starts an inline mapping whose other keys sit 2 deeper
                child_indent = indent + 2 + (len(item) - len(item.lstrip()))
                self.i -= 1
                self.raw[self.i] = " " * child_indent + item.lstrip()
                out.append(self._map(child_indent))
            else:
                out.append(self._value(item, indent))
        return out

    def _map(self, indent: int) -> dict:
        out: dict[str, Any] = {}
        while True:
            cur = self._next_indent()
            if cur != indent:
                break
            line = self._content(self.i).strip()
            if line.startswith("- "):
                break
            m = _KEY.match(line)
            if not m:
                raise YAMLError(f"line {self.i + 1}: cannot parse {line!r}")
            key = str(_scalar(m.group("key")))
            self.i += 1
            out[key] = self._value(m.group("val"), indent)
        return out
