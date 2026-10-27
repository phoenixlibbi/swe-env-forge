"""Test-log parsers: turn raw test output into ``{test_name: status}``.

Statuses are ``PASSED``, ``FAILED``, ``ERROR``, ``SKIPPED`` and ``XFAIL``.
:func:`is_pass` treats ``PASSED`` and ``XFAIL`` as passing, as SWE-bench does.

The Python parsers named ``pytest``, ``pytest-options``, ``pytest-v2``, ``django``,
``sympy``, ``seaborn`` and ``matplotlib`` reproduce SWE-bench's naming rules, so
the test names they return match the ``FAIL_TO_PASS`` / ``PASS_TO_PASS`` lists
of SWE-bench datasets. The other parsers cover Go, Jest/Vitest, Mocha, Cargo,
JUnit XML, .NET (TRX and console), TAP, RSpec, Minitest, GoogleTest and
Pro-style JSON. ``auto`` tries all of them and keeps the best result.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from typing import Callable

PASSED, FAILED, ERROR, SKIPPED, XFAIL = "PASSED", "FAILED", "ERROR", "SKIPPED", "XFAIL"
STATUSES = (FAILED, PASSED, SKIPPED, ERROR, XFAIL)

Parser = Callable[[str], dict[str, str]]
PARSERS: dict[str, Parser] = {}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\[\d+m")


def register(*names: str):
    def deco(fn: Parser) -> Parser:
        for n in names:
            PARSERS[n] = fn
        return fn
    return deco


def is_pass(status: str | None) -> bool:
    return status in (PASSED, XFAIL)


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


# ------------------------------------------------------------- python (SWE-bench)
@register("pytest")
def parse_pytest(log: str) -> dict[str, str]:
    """pytest ``-rA`` short summary lines (``PASSED path::test``), SWE-bench naming."""
    out: dict[str, str] = {}
    for line in log.split("\n"):
        if any(line.startswith(s) for s in STATUSES):
            if line.startswith(FAILED):
                line = " - ".join(line.split(" - ")[:-1]) if " - " in line else line
            parts = line.split()
            if len(parts) > 1:
                out[parts[1]] = parts[0]
    return out


@register("pytest-options")
def parse_pytest_options(log: str) -> dict[str, str]:
    """pytest with parametrize ids that contain paths (SWE-bench's requests / pylint / pydicom rule)."""
    option = re.compile(r"(.*?)\[(.*)\]")
    out: dict[str, str] = {}
    for line in log.split("\n"):
        if any(line.startswith(s) for s in STATUSES):
            if line.startswith(FAILED):
                line = line.replace(" - ", " ")
            parts = line.split()
            if len(parts) <= 1:
                continue
            m = option.search(parts[1])
            name = parts[1]
            if m:
                main, opt = m.groups()
                if opt.startswith("/") and not opt.startswith("//") and "*" not in opt:
                    opt = "/" + opt.split("/")[-1]
                name = f"{main}[{opt}]"
            out[name] = parts[0]
    return out


@register("pytest-v2")
def parse_pytest_v2(log: str) -> dict[str, str]:
    """Newer pytest output: strips colour codes, accepts names with spaces and ``name STATUS`` lines."""
    out: dict[str, str] = {}
    escapes = str.maketrans("", "", "".join(chr(c) for c in range(1, 32)))
    for line in log.split("\n"):
        line = re.sub(r"\[(\d+)m", "", line).translate(escapes)
        if any(line.startswith(s) for s in STATUSES):
            if line.startswith(FAILED) and " - " in line:
                line = " - ".join(line.split(" - ")[:-1])
            parts = line.split()
            if len(parts) >= 2:
                out[" ".join(parts[1:])] = parts[0]
        elif any(line.endswith(s) for s in STATUSES):
            parts = line.split()
            if len(parts) >= 2:
                out[parts[0]] = parts[1]
    progress = re.compile(r"^\[\s*\d+%]$")
    return {k: v for k, v in out.items() if not progress.match(k)}


@register("matplotlib")
def parse_matplotlib(log: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in log.split("\n"):
        line = line.replace("MouseButton.LEFT", "1").replace("MouseButton.RIGHT", "3")
        if any(line.startswith(s) for s in STATUSES):
            if line.startswith(FAILED):
                line = line.replace(" - ", " ")
            parts = line.split()
            if len(parts) > 1:
                out[parts[1]] = parts[0]
    return out


@register("seaborn")
def parse_seaborn(log: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in log.split("\n"):
        if line.startswith(FAILED):
            out[line.split()[1]] = FAILED
        elif f" {PASSED} " in line:
            parts = line.split()
            if parts[1] == PASSED:
                out[parts[0]] = PASSED
        elif line.startswith(PASSED):
            out[line.split()[1]] = PASSED
    return out


@register("django")
def parse_django(log: str) -> dict[str, str]:
    """Django's ``runtests.py --verbosity 2`` output (``test_x (mod.Class) ... ok``)."""
    out: dict[str, str] = {}
    prev = None
    for line in log.split("\n"):
        line = line.strip()
        if "--version is equivalent to version" in line:
            out["--version is equivalent to version"] = PASSED
        if " ... " in line:
            prev = line.split(" ... ")[0]
        for suffix in (" ... ok", " ... OK", " ...  OK"):
            if line.endswith(suffix):
                if line.startswith("Applying sites.0002_alter_domain_unique...test_no_migrations"):
                    line = line.split("...", 1)[-1].strip()
                out[line.rsplit(suffix, 1)[0]] = PASSED
                break
        if " ... skipped" in line:
            out[line.split(" ... skipped")[0]] = SKIPPED
        if line.endswith(" ... FAIL"):
            out[line.split(" ... FAIL")[0]] = FAILED
        if line.startswith("FAIL:") and len(line.split()) > 1:
            out[line.split()[1].strip()] = FAILED
        if line.endswith(" ... ERROR"):
            out[line.split(" ... ERROR")[0]] = ERROR
        if line.startswith("ERROR:") and len(line.split()) > 1:
            out[line.split()[1].strip()] = ERROR
        if line.lstrip().startswith("ok") and prev is not None:
            out[prev] = PASSED
    for pattern in (
        r"^(.*?)\s\.\.\.\sTesting\ against\ Django\ installed\ in\ ((?s:.*?))\ silenced\)\.\nok$",
        r"^(.*?)\s\.\.\.\sInternal\ Server\ Error:\ \/(.*)\/\nok$",
        r"^(.*?)\s\.\.\.\sSystem check identified no issues \(0 silenced\)\nok$",
    ):
        for m in re.finditer(pattern, log, re.MULTILINE):
            out[m.group(1)] = PASSED
    return out


@register("sympy")
def parse_sympy(log: str) -> dict[str, str]:
    """SymPy's ``bin/test -C --verbose`` output."""
    out: dict[str, str] = {}
    for m in re.findall(r"(_*) (.*)\.py:(.*) (_*)", log):
        out[f"{m[1]}.py:{m[2]}"] = FAILED
    for line in log.split("\n"):
        line = line.strip()
        if line.startswith("test_"):
            if line.endswith(" E"):
                out[line.split()[0]] = ERROR
            if line.endswith(" F"):
                out[line.split()[0]] = FAILED
            if line.endswith(" ok"):
                out[line.split()[0]] = PASSED
    return out


@register("pytest-verbose")
def parse_pytest_verbose(log: str) -> dict[str, str]:
    """``pytest -v`` progress lines: ``tests/test_x.py::test_a PASSED [ 50%]``."""
    out: dict[str, str] = {}
    rx = re.compile(r"^(\S+::\S.*?)\s+(PASSED|FAILED|ERROR|SKIPPED|XFAIL|XPASS)(?:\s+\[\s*\d+%\])?\s*$")
    for line in strip_ansi(log).split("\n"):
        m = rx.match(line.strip())
        if m:
            out[m.group(1)] = PASSED if m.group(2) == "XPASS" else m.group(2)
    return out


# -------------------------------------------------------------------------- go
@register("go", "go-json")
def parse_go(log: str) -> dict[str, str]:
    """``go test -json`` events, falling back to ``go test -v`` text (``--- PASS: TestX``)."""
    out: dict[str, str] = {}
    action = {"pass": PASSED, "fail": FAILED, "skip": SKIPPED}
    for line in log.split("\n"):
        line = line.strip()
        if line.startswith("{") and '"Action"' in line:
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("Test") and ev.get("Action") in action:
                out[ev["Test"]] = action[ev["Action"]]
    if out:
        return out
    rx = re.compile(r"^\s*--- (PASS|FAIL|SKIP): (\S+)")
    for line in strip_ansi(log).split("\n"):
        m = rx.match(line)
        if m:
            out[m.group(2)] = {"PASS": PASSED, "FAIL": FAILED, "SKIP": SKIPPED}[m.group(1)]
    return out


# ----------------------------------------------------------- javascript family
_JSON_START = re.compile(r"^[ \t]*\{", re.M)


def _json_objects(log: str) -> list:
    """JSON objects embedded in a log, each starting at the beginning of a line."""
    found, dec, pos = [], json.JSONDecoder(), 0
    while True:
        m = _JSON_START.search(log, pos)
        if not m:
            return found
        start = m.end() - 1
        try:
            obj, end = dec.raw_decode(log, start)
            found.append(obj)
            pos = end
        except json.JSONDecodeError:
            pos = start + 1


_JEST_STATUS = {"passed": PASSED, "failed": FAILED, "pending": SKIPPED, "skipped": SKIPPED,
                "todo": SKIPPED, "disabled": SKIPPED, "focused": PASSED}


@register("jest", "vitest")
def parse_jest(log: str) -> dict[str, str]:
    """Jest / Vitest ``--json`` reports (names: ``<file>::<full name>``), else verbose ✓ / ✕ lines."""
    out: dict[str, str] = {}
    for obj in _json_objects(log):
        if not isinstance(obj, dict) or "testResults" not in obj:
            continue
        for suite in obj.get("testResults") or []:
            fname = suite.get("name") or suite.get("testFilePath") or ""
            for a in suite.get("assertionResults") or []:
                full = a.get("fullName") or " ".join([*a.get("ancestorTitles", []), a.get("title", "")]).strip()
                out[f"{fname}::{full}" if fname else full] = _JEST_STATUS.get(a.get("status", ""), FAILED)
    if out:
        return out
    rx = re.compile(r"^\s*([✓✔√]|[✕✗×]|○ skipped|○)\s+(.+?)(?:\s+\(\d+(?:\.\d+)?\s*m?s\))?\s*$")
    for line in strip_ansi(log).split("\n"):
        m = rx.match(line)
        if m:
            mark = m.group(1)
            out[m.group(2)] = PASSED if mark in "✓✔√" else (SKIPPED if mark.startswith("○") else FAILED)
    return out


@register("mocha")
def parse_mocha(log: str) -> dict[str, str]:
    """Mocha ``--reporter json`` output (names: ``fullTitle``)."""
    out: dict[str, str] = {}
    for obj in _json_objects(log):
        if not isinstance(obj, dict) or not ({"passes", "failures", "tests"} & set(obj)):
            continue
        for t in obj.get("tests") or []:
            out[t.get("fullTitle", t.get("title", ""))] = FAILED if t.get("err") else PASSED
        for key, status in (("passes", PASSED), ("failures", FAILED), ("pending", SKIPPED)):
            for t in obj.get(key) or []:
                out[t.get("fullTitle", t.get("title", ""))] = status
    out.pop("", None)
    return out


@register("tap")
def parse_tap(log: str) -> dict[str, str]:
    out: dict[str, str] = {}
    rx = re.compile(r"^\s*(not ok|ok)\b(?:\s+\d+)?\s*(?:-\s*)?(.*?)(?:\s+#\s*(SKIP|TODO)\b.*)?$", re.I)
    for line in log.split("\n"):
        m = rx.match(line)
        if m and m.group(2):
            if m.group(3):
                out[m.group(2)] = SKIPPED
            else:
                out[m.group(2)] = PASSED if m.group(1).lower() == "ok" else FAILED
    return out


# ------------------------------------------------------------------------ rust
@register("cargo")
def parse_cargo(log: str) -> dict[str, str]:
    """``cargo test`` lines: ``test module::name ... ok | FAILED | ignored``."""
    out: dict[str, str] = {}
    rx = re.compile(r"^test (\S+)(?: - .*?)? \.\.\. (ok|FAILED|ignored|bench)")
    for line in strip_ansi(log).split("\n"):
        m = rx.match(line.strip())
        if m:
            out[m.group(1)] = {"ok": PASSED, "FAILED": FAILED, "ignored": SKIPPED, "bench": PASSED}[m.group(2)]
    return out


# ------------------------------------------------------------ xml: junit / trx
def _xml_docs(log: str) -> list[ET.Element]:
    docs = []
    for m in re.finditer(r"<(testsuites|testsuite|TestRun)\b.*?</\1>", log, re.S):
        try:
            docs.append(ET.fromstring(m.group(0)))
        except ET.ParseError:
            continue
    return docs


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


@register("junit")
def parse_junit(log: str) -> dict[str, str]:
    """JUnit XML (Maven Surefire, Gradle, pytest --junitxml, jest-junit, go-junit-report, dotnet JunitXml)."""
    out: dict[str, str] = {}
    for doc in _xml_docs(log):
        for tc in doc.iter():
            if _local(tc.tag) != "testcase":
                continue
            name, cls = tc.get("name", ""), tc.get("classname", "")
            full = f"{cls}.{name}" if cls and not name.startswith(cls) else name
            kids = {_local(k.tag) for k in tc}
            status = (FAILED if "failure" in kids else ERROR if "error" in kids
                      else SKIPPED if "skipped" in kids else PASSED)
            out[full] = status
    return out


@register("trx")
def parse_trx(log: str) -> dict[str, str]:
    """Visual Studio TRX (``dotnet test --logger trx``)."""
    out: dict[str, str] = {}
    status = {"passed": PASSED, "failed": FAILED, "notexecuted": SKIPPED, "inconclusive": SKIPPED,
              "error": ERROR, "timeout": FAILED, "aborted": ERROR}
    for doc in _xml_docs(log):
        for r in doc.iter():
            if _local(r.tag) == "UnitTestResult" and r.get("testName"):
                out[r.get("testName")] = status.get(r.get("outcome", "").lower(), FAILED)
    return out


@register("dotnet")
def parse_dotnet(log: str) -> dict[str, str]:
    """``dotnet test -v normal`` console lines: ``Passed Name [3 ms]`` / ``Failed Name [..]``."""
    out: dict[str, str] = {}
    rx = re.compile(r"^\s*(Passed|Failed|Skipped)\s+(.+?)(?:\s+\[[^\]]*\])?\s*$")
    for line in strip_ansi(log).split("\n"):
        m = rx.match(line)
        if m and not m.group(2).startswith("!"):
            out[m.group(2)] = {"Passed": PASSED, "Failed": FAILED, "Skipped": SKIPPED}[m.group(1)]
    return out


# ------------------------------------------------------------------- ruby / c++
@register("rspec")
def parse_rspec(log: str) -> dict[str, str]:
    """RSpec ``--format json`` (names: ``full_description``)."""
    out: dict[str, str] = {}
    for obj in _json_objects(log):
        if isinstance(obj, dict) and isinstance(obj.get("examples"), list):
            for ex in obj["examples"]:
                st = ex.get("status", "")
                out[ex.get("full_description") or ex.get("id", "")] = (
                    PASSED if st == "passed" else SKIPPED if st == "pending" else FAILED)
    out.pop("", None)
    return out


@register("minitest")
def parse_minitest(log: str) -> dict[str, str]:
    """Minitest ``--verbose`` lines: ``Suite#test_name = 0.00 s = .``."""
    out: dict[str, str] = {}
    rx = re.compile(r"^(\S+#\S+) = [\d.]+ s = ([.FES])\s*$")
    for line in strip_ansi(log).split("\n"):
        m = rx.match(line.strip())
        if m:
            out[m.group(1)] = {".": PASSED, "F": FAILED, "E": ERROR, "S": SKIPPED}[m.group(2)]
    return out


@register("gtest")
def parse_gtest(log: str) -> dict[str, str]:
    """GoogleTest: ``[       OK ] Suite.Test (0 ms)`` / ``[  FAILED  ] Suite.Test``."""
    out: dict[str, str] = {}
    rx = re.compile(r"^\[\s*(OK|FAILED|SKIPPED)\s*\]\s+(\S+\.\S+?)(?:,.*| \(\d+ ms\))?\s*$")
    for line in strip_ansi(log).split("\n"):
        m = rx.match(line.strip())
        if m:
            out[m.group(2)] = {"OK": PASSED, "FAILED": FAILED, "SKIPPED": SKIPPED}[m.group(1)]
    return out


# ---------------------------------------------------------------- pro / generic
@register("json", "pro")
def parse_json(log: str) -> dict[str, str]:
    """SWE-bench Pro parser output: ``{"tests": [{"name": ..., "status": "PASSED"}]}``."""
    out: dict[str, str] = {}
    for obj in _json_objects(log):
        if isinstance(obj, dict) and isinstance(obj.get("tests"), list):
            for t in obj["tests"]:
                if isinstance(t, dict) and "name" in t:
                    st = str(t.get("status", "")).upper()
                    out[t["name"]] = st if st in STATUSES else FAILED
    return out


# Order matters for ties in ``auto``: structured formats first.
_AUTO_ORDER = ("json", "jest", "mocha", "rspec", "junit", "trx", "go", "cargo", "pytest", "pytest-verbose",
               "django", "dotnet", "gtest", "minitest", "tap", "sympy")


@register("auto")
def parse_auto(log: str) -> dict[str, str]:
    best: dict[str, str] = {}
    for name in _AUTO_ORDER:
        try:
            res = PARSERS[name](log)
        except Exception:  # a parser must never take the harness down
            continue
        if len(res) > len(best):
            best = res
    return best


def parse_log(log: str, parser: str = "auto") -> dict[str, str]:
    if parser not in PARSERS:
        raise ValueError(f"unknown log parser {parser!r}; choose from {', '.join(sorted(PARSERS))}")
    return PARSERS[parser](log)
