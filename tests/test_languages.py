import pytest

from swe_env_forge.languages import (adds_skip_marker, definitions_in_line, detect_language, is_test_path,
                                   language_of, missing_symbols)


@pytest.mark.parametrize("path", [
    "tests/test_x.py", "pkg/test_util.py", "pkg/util_test.py", "conftest.py", "a/b/foo_test.go",
    "src/__tests__/x.js", "src/x.test.ts", "src/x.spec.tsx", "spec/models/user_spec.rb",
    "src/test/java/com/x/FooTest.java", "tests/A2A.UnitTests/Thing.cs", "Foo.Tests/BarTests.cs",
    "tests/integration.rs", "pkg/testdata/in.json", "app/FooTest.php",
])
def test_is_test_path_true(path):
    assert is_test_path(path)


@pytest.mark.parametrize("path", [
    "src/latest.py", "pkg/contest.go", "src/protest.ts", "lib/attestation.rb", "src/Foo.cs",
    "docs/testing-guide.md.txt", "src/main/java/com/x/Tester.java",
])
def test_is_test_path_false(path):
    assert not is_test_path(path)


def test_language_detection_prefers_source_files():
    assert language_of("a/b.tsx") == "typescript"
    assert detect_language(["src/a.go", "src/b.go", "x_test.go", "README.md"]) == "go"
    assert detect_language(["tests/test_a.py"]) == "python"
    assert detect_language(["README.md"]) == ""


@pytest.mark.parametrize("lang,line,name", [
    ("python", "    async def fetch_book(isbn):", "fetch_book"),
    ("python", "class Batch(Base):", "Batch"),
    ("go", "func (s *Server) HandleLogin(w http.ResponseWriter) {", "HandleLogin"),
    ("go", "type Config struct {", "Config"),
    ("javascript", "export async function getOrCreate() {", "getOrCreate"),
    ("javascript", "export const del = (keys) => {", "del"),
    ("typescript", "export interface CacheOptions {", "CacheOptions"),
    ("rust", "pub(crate) async fn parse_header(input: &str) -> Result<()> {", "parse_header"),
    ("java", "    public static List<String> splitWords(String s) {", "splitWords"),
    ("csharp", "    public async Task<int> CountAsync(CancellationToken ct)", "CountAsync"),
    ("ruby", "  def self.from_hash(h)", "from_hash"),
    ("php", "    public function handleRequest($r)", "handleRequest"),
])
def test_definitions(lang, line, name):
    assert definitions_in_line(line, lang)[0][1] == name


def test_control_flow_is_not_a_definition():
    assert definitions_in_line("    if (x) {", "javascript") == []


@pytest.mark.parametrize("log,sym", [
    ("ImportError: cannot import name 'apply_discount' from 'shop.cart'", "apply_discount"),
    ("AttributeError: 'Cart' object has no attribute 'apply_discount'", "apply_discount"),
    ("./x_test.go:12:3: undefined: ParseHeader", "ParseHeader"),
    ("error[E0425]: cannot find function `parse_header` in this scope", "parse_header"),
    ("Foo.cs(3,5): error CS0103: The name 'Render' does not exist in the current context", "Render"),
    ("  symbol:   method splitWords(java.lang.String)", "splitWords"),
    ("TypeError: cache.getOrCreate is not a function", "getOrCreate"),
    ("src/a.test.ts(1,10): error TS2305: Module '\"./cache\"' has no exported member 'del'.", "del"),
    ("NoMethodError: undefined method `from_hash' for Foo:Class", "from_hash"),
    ("error: 'make_widget' was not declared in this scope", "make_widget"),
])
def test_missing_symbols(log, sym):
    assert sym in [s for _k, s in missing_symbols(log)]


def test_missing_symbols_dedup_and_assertion_errors_ignored():
    log = "ImportError: cannot import name 'x'\nImportError: cannot import name 'x'\nAssertionError: 1 != 2"
    assert missing_symbols(log) == [("python-import", "x")]


@pytest.mark.parametrize("line", [
    "@pytest.mark.skip(reason='x')", "  it.skip('works', () => {", "    t.Skip()", "#[ignore]",
    "@Disabled", "  [Fact(Skip = \"later\")]", "xit('a', fn)", "    self.skipTest('no')",
])
def test_skip_markers(line):
    assert adds_skip_marker(line)


def test_not_skip_markers():
    assert not adds_skip_marker("skipped = compute()")
    assert not adds_skip_marker("def skip_header(self):")
