from swe_env_forge.grade import grade, tamper_flags
from swe_env_forge.sandbox import LocalSandbox
from swe_env_forge.validate import apply_validation, validate
from swe_env_forge.workspace import checkout_base, collect_diff


def validated(task, **kw):
    res = validate(task, backend="local", runs=2, **kw)
    return apply_validation(task, res), res


def test_validate_bug_fix(demo_tasks):
    _t, res = validated(demo_tasks["fix"])
    assert res.verdict == "valid", res
    assert res.fail_to_pass == ["tests/test_pricing.py::test_discount_rounds_half_up"]
    assert res.pass_to_pass == ["tests/test_pricing.py::test_format_price", "tests/test_pricing.py::test_no_discount"]
    assert not res.flaky and not res.warnings


def test_validate_flags_naming_leak(demo_tasks):
    _t, res = validated(demo_tasks["feature"])
    assert res.verdict == "valid" and "naming_leak" in res.warnings
    assert {"kind": "python-import", "symbol": "low_stock_items", "specified": False} in res.naming_errors
    _t2, strict = validated(demo_tasks["feature"], naming="invalid")
    assert strict.verdict == "invalid" and "naming_leak" in strict.reasons


def test_validate_detects_flaky_test(demo_tasks, reset_flaky_counter):
    _t, res = validated(demo_tasks["flaky"])
    assert res.flaky == ["tests/test_orders.py::test_order_ids_are_unique_under_load"]
    assert res.fail_to_pass == ["tests/test_orders.py::test_order_id_format"]
    assert any(w.startswith("flaky_tests") for w in res.warnings)


def test_grade_gold_and_empty(demo_tasks):
    t, _ = validated(demo_tasks["bulk"])
    gold = grade(t, t.gold_patch, backend="local")
    assert gold.resolved and gold.dense == 1.0 and gold.failure == "" and not gold.flags
    empty = grade(t, "", backend="local")
    assert not empty.resolved and empty.failure == "empty_patch" and empty.dense == 0.0


def _patch_from_edits(task, edits):
    with LocalSandbox() as sb:
        checkout_base(sb, task)
        for path, content in edits.items():
            sb.write_file(path, content)
        return collect_diff(sb, task.base_commit)


def test_grade_reverts_hidden_test_edits(demo_tasks):
    t, _ = validated(demo_tasks["fix"])
    cheat = _patch_from_edits(t, {"tests/test_pricing.py": "def test_discount_rounds_half_up():\n    pass\n"})
    r = grade(t, cheat, backend="local")
    assert not r.resolved
    assert r.reverted_test_files == ["tests/test_pricing.py"]
    assert "edits_hidden_test_file:tests/test_pricing.py" in r.flags


def test_base_run_unmasks_tests_that_only_failed_to_import(demo_tasks):
    # the new test file imports low_stock_items, so before the fix the whole file fails to import;
    # test_take and test_take_too_many passed at the base commit, so they are P2P, not F2P
    _t, res = validated(demo_tasks["feature"])
    assert res.fail_to_pass == ["tests/test_inventory.py::test_low_stock_items"]
    assert res.masked_f2p == ["tests/test_inventory.py::test_take", "tests/test_inventory.py::test_take_too_many"]
    assert set(res.masked_f2p) <= set(res.pass_to_pass)
    assert "f2p_masked_by_import_error:2" in res.warnings


def test_environment_incomplete_is_reported(demo_tasks):
    import copy

    t = copy.deepcopy(demo_tasks["fix"])
    t.test_patch = ("diff --git a/tests/test_env.py b/tests/test_env.py\nnew file mode 100644\n--- /dev/null\n"
                    "+++ b/tests/test_env.py\n@@ -0,0 +1,4 @@\n+import freezegun_not_installed\n+\n+\n"
                    "+def test_needs_dependency():\n")
    t.test_patch = t.test_patch.replace("@@ -0,0 +1,4 @@", "@@ -0,0 +1,5 @@") + "+    assert freezegun_not_installed\n"
    res = validate(t, backend="local", runs=1)
    assert res.verdict == "invalid" and res.reasons == ["environment_incomplete:freezegun_not_installed"]


def test_grade_partial_credit_is_dense(demo_tasks):
    t, _ = validated(demo_tasks["feature"], base_run=False)   # keep 3 F2P tests to exercise partial credit
    assert len(t.fail_to_pass) == 3
    base_inv = t.gold_patch  # noqa: F841 - documents what the right answer is
    wrong_order = _patch_from_edits(t, {"minishop/inventory.py": (
        "class Inventory:\n    def __init__(self, stock=None):\n        self.stock = dict(stock or {})\n\n"
        "    def take(self, name, qty=1):\n        if self.stock.get(name, 0) < qty:\n"
        "            raise ValueError(f'not enough {name}')\n        self.stock[name] -= qty\n\n\n"
        "def low_stock_items(inventory, threshold=5):\n"
        "    return [n for n, q in inventory.stock.items() if q <= threshold]\n")})
    r = grade(t, wrong_order, backend="local")
    assert not r.resolved and r.failure == "partial"
    assert r.f2p_rate == round(2 / 3, 4) and r.dense == round(2 / 3, 4)


def test_grade_regression(demo_tasks):
    t, _ = validated(demo_tasks["fix"])
    # fixes the rounding but breaks formatting -> F2P passes, a P2P test fails
    gold_text = t.gold_patch.replace('return f"${cents // 100}.{cents % 100:02d}"', 'return str(cents)')
    regress = _patch_from_edits(t, {"minishop/pricing.py": (
        '"""Prices are stored in cents."""\n\n\ndef apply_discount(cents, percent):\n'
        '    return cents - (cents * percent + 50) // 100\n\n\ndef format_price(cents):\n    return str(cents)\n')})
    assert gold_text  # unused, kept for readability
    r = grade(t, regress, backend="local")
    assert not r.resolved and r.failure == "regression"
    assert r.p2p_failed == ["tests/test_pricing.py::test_format_price"]
    assert r.f2p_rate == 1.0 and r.dense == 0.5


def test_grade_garbage_patch(demo_tasks):
    t, _ = validated(demo_tasks["fix"])
    r = grade(t, "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-nope\n+yes\n", backend="local")
    assert r.failure == "patch_apply_error" and not r.patch_applied


def test_grade_without_f2p_is_a_harness_error(demo_tasks):
    import copy

    t = copy.deepcopy(demo_tasks["fix"])
    t.fail_to_pass = []
    assert grade(t, t.gold_patch, backend="local").failure == "harness_error"


def test_strict_mode_and_flags():
    patch = ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,2 @@\n x\n"
             "+@pytest.mark.skip\n"
             "diff --git a/tests/test_b.py b/tests/test_b.py\ndeleted file mode 100644\n--- a/tests/test_b.py\n"
             "+++ /dev/null\n@@ -1 +0,0 @@\n-x\n"
             "diff --git a/conftest.py b/conftest.py\n--- a/conftest.py\n+++ b/conftest.py\n@@ -1 +1 @@\n-a\n+b\n")
    flags, revert = tamper_flags(patch, {"tests/test_hidden.py"})
    assert revert == ["tests/test_b.py", "conftest.py"]
    assert "adds_skip_marker:src/a.py" in flags
    assert "deletes_test_file:tests/test_b.py" in flags
    assert "edits_test_runner_config:conftest.py" in flags


def test_tamper_flags_respect_source_files_under_test_dirs():
    patch = ("diff --git a/django/test/utils.py b/django/test/utils.py\n--- a/django/test/utils.py\n"
             "+++ b/django/test/utils.py\n@@ -1 +1 @@\n-a\n+b\n")
    assert tamper_flags(patch, set())[1] == ["django/test/utils.py"]
    assert tamper_flags(patch, set(), {"django/test/utils.py"})[1] == []
    toml = ('diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n+++ b/pyproject.toml\n'
            '@@ -1 +1,2 @@\n x\n+addopts = "-p plug"\n')
    dep = ('diff --git a/pyproject.toml b/pyproject.toml\n--- a/pyproject.toml\n+++ b/pyproject.toml\n'
           '@@ -1 +1,2 @@\n x\n+dependencies = ["attrs"]\n')
    assert tamper_flags(toml, set())[0] == ["edits_test_config:pyproject.toml"]
    assert tamper_flags(dep, set())[0] == []


def test_changed_test_functions_maps_hunks_to_enclosing_tests():
    from swe_env_forge.sandbox import LocalSandbox
    from swe_env_forge.task import Task
    from swe_env_forge.validate import _test_name_matches, changed_test_functions

    new_file = ("import hashlib\nfrom pkg import _lazy\n\n\nclass TestS:\n    def test_iter(self):\n"
                "        x = 1\n        assert x == _lazy\n\n    def test_other(self):\n        assert True\n")
    patch = ("diff --git a/tests/test_s.py b/tests/test_s.py\n--- a/tests/test_s.py\n+++ b/tests/test_s.py\n"
             "@@ -1,2 +1,3 @@\n import hashlib\n+from pkg import _lazy\n \n@@ -5,3 +6,3 @@ class TestS:\n"
             "     def test_iter(self):\n         x = 1\n-        assert x == hashlib.sha1\n+        assert x == _lazy\n")
    t = Task(instance_id="i", repo="r", base_commit="b", test_patch=patch)
    with LocalSandbox() as sb:
        sb.write_file(f"{sb.workdir}/tests/test_s.py", new_file)
        got = changed_test_functions(sb, t)
    assert got == {"tests/test_s.py": {"test_iter"}}   # the import line has no enclosing test
    assert _test_name_matches("tests/test_s.py::TestS::test_iter[a-b]", got["tests/test_s.py"])
    assert not _test_name_matches("tests/test_s.py::TestS::test_other", got["tests/test_s.py"])


def test_results_carry_an_environment_fingerprint(demo_tasks):
    from swe_env_forge import __version__

    t, res = validated(demo_tasks["fix"])
    assert res.environment["sweforge_version"] == __version__ and res.environment["start_ref"] == t.base_commit
    assert t.metadata["validation"]["environment"]["backend"] == "LocalSandbox"
    g = grade(t, t.gold_patch, backend="local")
    assert g.metadata["environment"]["sweforge_version"] == __version__


def test_image_tags_follow_the_dockerfile():
    from swe_env_forge.task import Task
    from swe_env_forge.workspace import image_tag

    a = Task.from_dict({"instance_id": "O__R-1", "repo": "o/r", "base_commit": "b", "env": {"dockerfile": "FROM a\n"}})
    b = Task.from_dict({"instance_id": "O__R-1", "repo": "o/r", "base_commit": "b", "env": {"dockerfile": "FROM b\n"}})
    assert image_tag(a).startswith("sweforge/o__r-1:") and image_tag(a) != image_tag(b)
    assert image_tag(a) == image_tag(Task.from_dict(a.to_dict()))
