from pathlib import Path

import pytest

from swe_env_kit.logparsers import PARSERS, is_pass, parse_log

LOGS = Path(__file__).parent / "fixtures" / "logs"


def load(name):
    return (LOGS / name).read_text()


EXPECTED = {
    "pytest_ra.log": ("pytest", {
        "tests/test_cart.py::test_total": "PASSED", "tests/test_cart.py::test_empty[a-b]": "PASSED",
        "tests/test_cart.py::test_future": "XFAIL", "tests/test_cart.py::test_discount": "FAILED",
        "tests/test_cart.py::test_db": "ERROR"}),
    "django.log": ("django", {
        "test_add (model_fields.test_integer.IntegerFieldTests)": "PASSED",
        "test_bad (model_fields.test_integer.IntegerFieldTests)": "FAILED",
        "test_err (model_fields.test_integer.IntegerFieldTests)": "ERROR",
        "test_skip (model_fields.test_integer.IntegerFieldTests)": "SKIPPED",
        "test_multi (model_fields.test_integer.IntegerFieldTests)": "PASSED"}),
    "sympy.log": ("sympy", {"test_printmethod": "PASSED", "test_Abs": "FAILED", "test_Add": "ERROR"}),
    "go_json.log": ("go", {"TestTotal": "PASSED", "TestDiscount/ten_percent": "FAILED", "TestSlow": "SKIPPED"}),
    "go_verbose.log": ("go", {"TestTotal": "PASSED", "TestDiscount/ten_percent": "FAILED", "TestDiscount": "FAILED"}),
    "jest.log": ("jest", {"/app/src/cart.test.ts::Cart totals": "PASSED",
                          "/app/src/cart.test.ts::Cart discounts": "FAILED",
                          "/app/src/cart.test.ts::later": "SKIPPED"}),
    "jest_verbose.log": ("jest", {"totals": "PASSED", "discounts": "FAILED", "later": "SKIPPED"}),
    "mocha.log": ("mocha", {"Cart totals": "PASSED", "Cart discounts": "FAILED", "Cart later": "SKIPPED"}),
    "cargo.log": ("cargo", {"cart::tests::totals": "PASSED", "cart::tests::discounts": "FAILED",
                            "cart::tests::slow": "SKIPPED", "src/lib.rs": "PASSED"}),
    "junit.log": ("junit", {"com.x.CartTest.totals": "PASSED", "com.x.CartTest.discounts": "FAILED",
                            "com.x.CartTest.slow": "SKIPPED", "com.x.CartTest.db": "ERROR"}),
    "trx.log": ("trx", {"Shop.Tests.CartTests.Totals": "PASSED", "Shop.Tests.CartTests.Discounts": "FAILED",
                        "Shop.Tests.CartTests.Slow": "SKIPPED"}),
    "dotnet.log": ("dotnet", {"Shop.Tests.CartTests.Totals": "PASSED", "Shop.Tests.CartTests.Discounts": "FAILED",
                              "Shop.Tests.CartTests.Slow": "SKIPPED"}),
    "tap.log": ("tap", {"totals": "PASSED", "discounts": "FAILED", "slow": "SKIPPED"}),
    "rspec.log": ("rspec", {"Cart totals": "PASSED", "Cart discounts": "FAILED", "Cart later": "SKIPPED"}),
    "minitest.log": ("minitest", {"CartTest#test_totals": "PASSED", "CartTest#test_discounts": "FAILED",
                                  "CartTest#test_db": "ERROR"}),
    "gtest.log": ("gtest", {"CartTest.Totals": "PASSED", "CartTest.Discounts": "FAILED", "CartTest.Slow": "SKIPPED"}),
    "pro.log": ("json", {"test/user.js | User | should exist": "PASSED", "test/user.js | User | should fail": "FAILED"}),
}


@pytest.mark.parametrize("fname", sorted(EXPECTED))
def test_named_parser(fname):
    parser, expected = EXPECTED[fname]
    assert parse_log(load(fname), parser) == expected


@pytest.mark.parametrize("fname", sorted(set(EXPECTED) - {"sympy.log"}))
def test_auto_finds_the_same_results(fname):
    _parser, expected = EXPECTED[fname]
    assert parse_log(load(fname), "auto") == expected


def test_cargo_doctest_name_is_kept_whole_enough():
    # doctest lines are "test src/lib.rs - cart::Cart::total (line 10) ... ok"
    assert parse_log(load("cargo.log"), "cargo")["src/lib.rs"] == "PASSED"


def test_pytest_v2_handles_colour_and_trailing_status():
    log = "\x1b[32mPASSED\x1b[0m tests/a.py::test_x\ntests/b.py::test_y FAILED\n[ 50%] PASSED"
    out = parse_log(log, "pytest-v2")
    assert out["tests/a.py::test_x"] == "PASSED" and out["tests/b.py::test_y"] == "FAILED"


def test_pytest_options_shortens_path_params():
    out = parse_log("PASSED tests/t.py::test_load[/very/long/path/data.csv]", "pytest-options")
    assert out == {"tests/t.py::test_load[/data.csv]": "PASSED"}


def test_pytest_verbose():
    log = "tests/t.py::test_a PASSED  [ 50%]\ntests/t.py::test_b[x y] FAILED [100%]\n"
    assert parse_log(log, "pytest-verbose") == {"tests/t.py::test_a": "PASSED", "tests/t.py::test_b[x y]": "FAILED"}


def test_last_result_wins_for_repeated_tests():
    assert parse_log("FAILED t.py::a - x\nPASSED t.py::a\n", "pytest") == {"t.py::a": "PASSED"}


def test_garbage_and_empty_logs():
    for name in PARSERS:
        assert parse_log("", name) == {}
    assert parse_log("{not json\n<testsuite><oops", "auto") == {}


def test_unknown_parser():
    with pytest.raises(ValueError, match="unknown log parser"):
        parse_log("x", "nope")


def test_is_pass():
    assert is_pass("PASSED") and is_pass("XFAIL")
    assert not is_pass("FAILED") and not is_pass(None) and not is_pass("SKIPPED")
