"""Per-language knowledge: file types, test paths, definitions, missing-symbol errors, skip markers."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import PurePosixPath

EXTENSIONS: dict[str, str] = {
    ".py": "python", ".pyi": "python",
    ".go": "go",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "typescript", ".mts": "typescript", ".cts": "typescript",
    ".rs": "rust",
    ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".scala": "scala",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c", ".h": "c",
    ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp", ".hh": "cpp", ".hxx": "cpp",
    ".swift": "swift",
}

# Files that never count as source code when measuring a fix.
NON_CODE_SUFFIXES = (".md", ".rst", ".txt", ".adoc", ".json", ".yml", ".yaml", ".toml", ".ini",
                     ".cfg", ".lock", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".csv")

TEST_DIRS = {"test", "tests", "testing", "__tests__", "spec", "specs", "e2e", "testdata",
             "test-data", "test_data", "__snapshots__", "__mocks__", "unittests", "unittest",
             "integration-tests", "integration_tests"}

_TEST_FILE_RES = [re.compile(p) for p in (
    r"^test_.*\.py$", r"^.*_test\.py$", r"^conftest\.py$", r"^tests?\.py$",
    r"^.*_test\.go$",
    r"^.*\.(test|spec)\.[cm]?[jt]sx?$",
    r"^.*_(spec|test)\.rb$",
    r"^.*(Test|Tests|IT|TestCase)\.(java|kt|scala)$",
    r"^.*(Test|Tests)\.cs$",
    r"^.*Test\.php$",
    r"^.*_test\.(c|cc|cpp)$", r"^test_.*\.(c|cc|cpp)$",
    r"^.*Tests?\.swift$",
)]


def is_test_file_name(path: str) -> bool:
    """True when the file *name* marks a test (``test_x.py``, ``x_test.go``, ``x.spec.ts``, ``FooTests.cs``).

    Stricter than :func:`is_test_path`, which also accepts any file under a ``test/`` directory;
    that would include framework code such as ``django/test/utils.py``.
    """
    return any(r.match(PurePosixPath(path).name) for r in _TEST_FILE_RES)


def language_of(path: str) -> str | None:
    return EXTENSIONS.get(PurePosixPath(path).suffix.lower())


def is_test_path(path: str) -> bool:
    """True for files that belong to a test suite (the test-patch side of a diff)."""
    p = PurePosixPath(path)
    for part in p.parts[:-1]:
        low = part.lower()
        if low in TEST_DIRS:
            return True
        # .NET test projects (Foo.Tests, Foo.UnitTests) and Swift/ObjC test targets (FooTests)
        if re.search(r"(\.|^)(unit|integration)?tests?$", low) and part[:1].isupper():
            return True
        if re.search(r"[A-Za-z](Tests|UnitTests|IntegrationTests)$", part):
            return True
    return any(r.match(p.name) for r in _TEST_FILE_RES)


def is_code_path(path: str) -> bool:
    return language_of(path) is not None and not path.lower().endswith(NON_CODE_SUFFIXES)


def detect_language(paths: list[str]) -> str:
    """Most common language among non-test code files (falls back to test files)."""
    src = Counter(language_of(p) for p in paths if is_code_path(p) and not is_test_path(p))
    src.pop(None, None)
    if not src:
        src = Counter(language_of(p) for p in paths if is_code_path(p))
        src.pop(None, None)
    return src.most_common(1)[0][0] if src else ""


# ----------------------------------------------------------------- definitions
# Each regex runs on one source line and must expose a named group "name".
_DEF = {
    "python": [
        ("function", r"^\s*(?:async\s+)?def\s+(?P<name>[A-Za-z_]\w*)\s*\("),
        ("class", r"^\s*class\s+(?P<name>[A-Za-z_]\w*)\s*[(:]"),
    ],
    "go": [
        ("function", r"^func\s+(?:\([^)]*\)\s*)?(?P<name>[A-Za-z_]\w*)\s*[\[(]"),
        ("type", r"^type\s+(?P<name>[A-Za-z_]\w*)\s+(?:struct|interface|func|\w)"),
    ],
    "javascript": [
        ("function", r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(?P<name>[A-Za-z_$][\w$]*)\s*\("),
        ("class", r"^\s*(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(?P<name>[A-Za-z_$][\w$]*)"),
        ("const", r"^\s*(?:export\s+)?(?:const|let|var)\s+(?P<name>[A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function|\(|[A-Za-z_$][\w$]*\s*=>)"),
        ("method", r"^\s*(?:static\s+|async\s+|public\s+|private\s+|protected\s+|readonly\s+)*(?P<name>[A-Za-z_$][\w$]*)\s*\([^)]*\)\s*(?::\s*[^{=]+)?\{\s*$"),
        ("property-function", r"^\s*(?P<name>[A-Za-z_$][\w$]*)\s*:\s*(?:async\s*)?function\b"),
    ],
    "rust": [
        ("function", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:const\s+|async\s+|unsafe\s+|extern\s+\"[^\"]*\"\s+)*fn\s+(?P<name>[A-Za-z_]\w*)"),
        ("type", r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:struct|enum|trait|type|union)\s+(?P<name>[A-Za-z_]\w*)"),
    ],
    "java": [
        ("type", r"^\s*(?:(?:public|protected|private|static|final|abstract|sealed)\s+)*(?:class|interface|enum|record|@interface)\s+(?P<name>[A-Za-z_]\w*)"),
        ("method", r"^\s*(?:@\w+\s+)*(?:(?:public|protected|private|static|final|abstract|synchronized|default|native)\s+)+[\w<>\[\],.? ]+\s+(?P<name>[A-Za-z_]\w*)\s*\("),
    ],
    "csharp": [
        ("type", r"^\s*(?:(?:public|internal|protected|private|static|sealed|abstract|partial|readonly)\s+)*(?:class|interface|enum|record|struct)\s+(?P<name>[A-Za-z_]\w*)"),
        ("method", r"^\s*(?:(?:public|internal|protected|private|static|virtual|override|async|sealed|abstract|extern|new)\s+)+[\w<>\[\],.?() ]+\s+(?P<name>[A-Za-z_]\w*)\s*[(<]"),
    ],
    "ruby": [
        ("method", r"^\s*def\s+(?:self\.)?(?P<name>[A-Za-z_]\w*[?!=]?)"),
        ("type", r"^\s*(?:class|module)\s+(?P<name>[A-Z]\w*)"),
    ],
    "php": [
        ("function", r"^\s*(?:(?:public|protected|private|static|final|abstract)\s+)*function\s+&?(?P<name>[A-Za-z_]\w*)\s*\("),
        ("type", r"^\s*(?:(?:final|abstract|readonly)\s+)*(?:class|interface|trait|enum)\s+(?P<name>[A-Za-z_]\w*)"),
    ],
    "c": [
        ("function", r"^(?:static\s+|inline\s+|extern\s+)*[A-Za-z_][\w\s\*]*?\b(?P<name>[A-Za-z_]\w*)\s*\([^;]*$"),
        ("type", r"^\s*(?:typedef\s+)?(?:struct|enum|union)\s+(?P<name>[A-Za-z_]\w*)\s*\{?"),
    ],
}
_DEF["typescript"] = _DEF["javascript"] + [
    ("type", r"^\s*(?:export\s+)?(?:declare\s+)?(?:interface|type|enum)\s+(?P<name>[A-Za-z_$][\w$]*)"),
]
_DEF["kotlin"] = [
    ("function", r"^\s*(?:(?:public|internal|private|protected|override|suspend|inline|open)\s+)*fun\s+(?:<[^>]*>\s*)?(?:[\w.]+\.)?(?P<name>[A-Za-z_]\w*)\s*\("),
    ("type", r"^\s*(?:(?:public|internal|private|data|sealed|open|abstract|enum)\s+)*(?:class|interface|object)\s+(?P<name>[A-Za-z_]\w*)"),
]
_DEF["scala"] = [
    ("function", r"^\s*(?:(?:override|private|protected|final)\s+)*def\s+(?P<name>[A-Za-z_]\w*)"),
    ("type", r"^\s*(?:(?:case|abstract|sealed|final)\s+)*(?:class|trait|object)\s+(?P<name>[A-Za-z_]\w*)"),
]
_DEF["cpp"] = _DEF["c"] + [("type", r"^\s*(?:template\s*<[^>]*>\s*)?(?:class|struct)\s+(?P<name>[A-Za-z_]\w*)")]
_DEF["swift"] = [
    ("function", r"^\s*(?:(?:public|private|internal|fileprivate|open|static|override|mutating)\s+)*func\s+(?P<name>[A-Za-z_]\w*)"),
    ("type", r"^\s*(?:(?:public|private|internal|final|open)\s+)*(?:class|struct|enum|protocol|actor)\s+(?P<name>[A-Za-z_]\w*)"),
]
DEFINITION_PATTERNS = {lang: [(kind, re.compile(rx)) for kind, rx in pats] for lang, pats in _DEF.items()}

_KEYWORDS = {"if", "for", "while", "switch", "catch", "return", "function", "else", "new", "throw",
             "typeof", "await", "sizeof", "do", "try", "with", "using", "lock", "foreach", "elif",
             "constructor", "super", "this", "match", "loop", "main", "init"}


def definitions_in_line(line: str, language: str) -> list[tuple[str, str]]:
    """(kind, name) pairs defined on a single source line."""
    found = []
    for kind, rx in DEFINITION_PATTERNS.get(language, ()):
        m = rx.match(line)
        if m and m.group("name") not in _KEYWORDS:
            found.append((kind, m.group("name")))
            break
    return found


# ----------------------------------------------------- missing-symbol errors
# Errors meaning "the test asked for a name that does not exist (yet)". On a
# pre-fix run these indicate the test depends on names invented by the fix.
_MISSING = [
    ("python-import", r"ImportError: cannot import name '(?P<sym>[\w.]+)'"),
    ("python-module", r"ModuleNotFoundError: No module named '(?P<sym>[\w.]+)'"),
    ("python-attribute", r"AttributeError: .*?has no attribute '(?P<sym>\w+)'"),
    ("python-name", r"NameError: name '(?P<sym>\w+)' is not defined"),
    ("go-undefined", r"undefined: (?P<sym>[\w.]+)"),
    ("go-no-member", r"has no field or method (?P<sym>\w+)"),
    ("rust-E0425", r"error\[E0425\]: cannot find (?:value|function|macro) `(?P<sym>\w+)`"),
    ("rust-E0412", r"error\[E0412\]: cannot find type `(?P<sym>\w+)`"),
    ("rust-E0432", r"error\[E0432\]: unresolved import `(?P<sym>[\w:]+)`"),
    ("rust-E0433", r"error\[E0433\]: failed to resolve: [^`\n]*`(?P<sym>[\w:]+)`"),
    ("rust-E0599", r"error\[E0599\]: no (?:method|function|associated item|variant)[\w ]* named `(?P<sym>\w+)`"),
    ("java-symbol", r"symbol:\s+(?:method|class|variable|constructor|static)\s+(?P<sym>\w+)"),
    ("kotlin-unresolved", r"Unresolved reference:? '?(?P<sym>\w+)"),
    ("csharp-CS0103", r"error CS0103: The name '(?P<sym>\w+)' does not exist"),
    ("csharp-CS0117", r"error CS0117: '[^']+' does not contain a definition for '(?P<sym>\w+)'"),
    ("csharp-CS1061", r"error CS1061: '[^']+' does not contain a definition for '(?P<sym>\w+)'"),
    ("csharp-CS0246", r"error CS0246: The type or namespace name '(?P<sym>\w+)'"),
    ("csharp-CS0234", r"error CS0234: The type or namespace name '(?P<sym>\w+)' does not exist"),
    ("js-not-a-function", r"TypeError: (?:[\w$.]+\.)?(?P<sym>[\w$]+) is not a function"),
    ("js-no-export", r"does not provide an export named '(?P<sym>[\w$]+)'"),
    ("js-module", r"Cannot find module '(?P<sym>[^']+)'"),
    ("js-reference", r"ReferenceError: (?P<sym>[\w$]+) is not defined"),
    ("ts-TS2305", r"TS2305: Module '[^']+' has no exported member '(?P<sym>[\w$]+)'"),
    ("ts-TS2339", r"TS2339: Property '(?P<sym>[\w$]+)' does not exist"),
    ("ts-TS2304", r"TS2304: Cannot find name '(?P<sym>[\w$]+)'"),
    ("ts-TS2307", r"TS2307: Cannot find module '(?P<sym>[^']+)'"),
    ("ruby-constant", r"NameError: uninitialized constant (?P<sym>[\w:]+)"),
    ("ruby-method", r"NoMethodError: undefined method [`'](?P<sym>\w+[?!=]?)'"),
    ("ruby-load", r"cannot load such file -- (?P<sym>\S+)"),
    ("php-undefined", r"Call to undefined (?:method|function) (?P<sym>[\w\\:]+)\("),
    ("php-class", r"Class \"(?P<sym>[\w\\]+)\" not found"),
    ("c-undeclared", r"error: '(?P<sym>\w+)' was not declared in this scope"),
    ("c-undeclared-clang", r"error: use of undeclared identifier '(?P<sym>\w+)'"),
    ("c-no-member", r"(?:has no member named|no member named) '(?P<sym>\w+)'"),
    ("c-missing-header", r"fatal error: (?P<sym>[\w./-]+): No such file or directory"),
]
MISSING_SYMBOL_PATTERNS = [(pid, re.compile(rx)) for pid, rx in _MISSING]


def missing_symbols(log: str) -> list[tuple[str, str]]:
    """(error_kind, symbol) pairs for every 'name does not exist' error in a log, de-duplicated."""
    seen: dict[tuple[str, str], None] = {}
    for pid, rx in MISSING_SYMBOL_PATTERNS:
        for m in rx.finditer(log):
            seen.setdefault((pid, m.group("sym")), None)
    return list(seen)


# -------------------------------------------------------------- skip markers
SKIP_MARKER_RE = re.compile(
    r"@pytest\.mark\.(?:skip|skipif|xfail)\b|@unittest\.skip|\bpytest\.skip\(|\bself\.skipTest\("
    r"|\b(?:it|test|describe|context)\.(?:skip|todo)\(|\b(?:xit|xdescribe|xtest)\("
    r"|\bt\.Skip(?:Now|f)?\(|#\[ignore\]|@Ignore\b|@Disabled\b|\[Ignore\b|\bSkip\s*=\s*\""
    r"|\bmarkTestSkipped\(|^\s*(?:skip|pending)\b(?:\s|$|\()|\bpytest\.xfail\("
)


def adds_skip_marker(line: str) -> bool:
    return bool(SKIP_MARKER_RE.search(line))
