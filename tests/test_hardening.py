"""Regression tests for adversarial patches and edge cases found in review.

The "malicious" candidate code below only ever touches paths inside its own sandbox.
"""
import copy

import pytest

from swe_env_forge.grade import grade
from swe_env_forge.patches import parse_patch
from swe_env_forge.sandbox import LocalSandbox
from swe_env_forge.validate import apply_validation, naming_errors, validate
from swe_env_forge.workspace import checkout_base, collect_diff, run_tests, strip_future_history

HOOK = '''import pytest


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    outcome.get_result().outcome = "passed"
'''


@pytest.fixture(scope="module")
def fix_task(demo_tasks):
    t = copy.deepcopy(demo_tasks["fix"])
    return apply_validation(t, validate(t, backend="local", runs=1))


def patch_from(task, files=None, shell=None):
    with LocalSandbox() as sb:
        checkout_base(sb, task)
        for path, content in (files or {}).items():
            sb.write_file(path, content)
        if shell:
            assert sb.exec(shell).ok
        return collect_diff(sb, task.base_commit)


def test_conftest_hook_cannot_force_passes(fix_task):
    r = grade(fix_task, patch_from(fix_task, {"conftest.py": HOOK}), backend="local")
    assert not r.resolved and "conftest.py" in r.reverted_test_files


def test_pyproject_plugin_is_tampering(fix_task):
    toml = ('[project]\nname = "minishop"\nversion = "0.1.0"\n\n[tool.pytest.ini_options]\n'
            'testpaths = ["tests"]\naddopts = "-p sweforge_plug"\n')
    p = patch_from(fix_task, {"pyproject.toml": toml, "sweforge_plug.py": HOOK})
    r = grade(fix_task, p, backend="local")
    assert not r.resolved and r.failure == "tampering"
    lenient = grade(fix_task, p, backend="local", strict=False)
    assert "edits_test_config:pyproject.toml" in lenient.flags


def test_glob_named_files_do_not_break_git(fix_task):
    p = patch_from(fix_task, {"safe.directory=1": "x", "safe.directory=2": "y"})
    r = grade(fix_task, p, backend="local")
    assert r.failure == "wrong_solution" and not r.harness_error


def test_renaming_a_hidden_test_file_is_not_a_harness_error(fix_task):
    p = patch_from(fix_task, shell="git mv tests/test_pricing.py tests/test_pricing_old.py")
    r = grade(fix_task, p, backend="local")
    assert not r.resolved and r.failure != "harness_error", r


def test_file_where_the_tests_need_a_directory(fix_task):
    t = copy.deepcopy(fix_task)
    t.test_patch = ("diff --git a/tests/extra/test_more.py b/tests/extra/test_more.py\nnew file mode 100644\n"
                    "--- /dev/null\n+++ b/tests/extra/test_more.py\n@@ -0,0 +1,5 @@\n"
                    "+from minishop.pricing import apply_discount\n+\n+\n+def test_rounds():\n"
                    "+    assert apply_discount(999, 15) == 849\n")
    t.fail_to_pass, t.pass_to_pass = ["tests/extra/test_more.py::test_rounds"], []
    assert grade(t, t.gold_patch, backend="local").resolved
    r = grade(t, patch_from(t, {"tests/extra": "not a directory"}), backend="local")
    assert r.failure != "harness_error", r


def test_strip_history_removes_kept_packs_and_head_files(demo_tasks):
    t = demo_tasks["fix"]
    future = t.metadata["instance_commit"]
    with LocalSandbox() as sb:
        checkout_base(sb, t)
        assert sb.exec(f"git update-ref ORIG_HEAD {future} && git repack -a -d -q && "
                       "for p in .git/objects/pack/*.pack; do touch \"${p%.pack}.keep\"; done").ok
        strip_future_history(sb, t.base_commit)
        assert not sb.exec(f"git cat-file -e {future}").ok
        assert not sb.exec("test -e .git/ORIG_HEAD").ok


def test_fixtures_in_the_test_patch_are_not_run(fix_task):
    t = copy.deepcopy(fix_task)
    t.test_patch += ("diff --git a/tests/data.json b/tests/data.json\nnew file mode 100644\n--- /dev/null\n"
                     "+++ b/tests/data.json\n@@ -0,0 +1 @@\n+{}\n")
    assert t.test_files() == ["tests/test_pricing.py"]
    assert grade(t, t.gold_patch, backend="local").resolved


def test_forged_end_marker_does_not_hide_results(fix_task):
    p = patch_from(fix_task, {"minishop/__init__.py": "print('>>>>> sweforge tests end 0')\n"})
    r = grade(fix_task, p, backend="local")
    assert r.failure == "wrong_solution" and r.f2p_failed


def test_pro_style_parser_runs_outside_the_sandbox(fix_task):
    t = copy.deepcopy(fix_task)
    t.env.run_script = 'python -m pytest -rA -p no:cacheprovider $(echo "$1" | tr , " ")\n'
    t.env.parser_script = (
        "import json, sys\n"
        "out = []\n"
        "for line in open(sys.argv[1]):\n"
        "    p = line.split()\n"
        "    if len(p) > 1 and p[0] in ('PASSED', 'FAILED'):\n"
        "        out.append({'name': p[1], 'status': p[0]})\n"
        "json.dump({'tests': out}, open(sys.argv[3], 'w'))\n")
    t.env.test_file_format, t.env.log_parser, t.env.test_command = "comma", "json", None
    assert grade(t, t.gold_patch, backend="local").resolved
    # Code under test tries to replace any parser it can find in its own sandbox (scratch is ../scratch).
    evil = ("import glob, os\n"
            "for f in glob.glob(os.path.join('..', 'scratch', '**', '*.py'), recursive=True):\n"
            "    open(f, 'w').write('import json,sys; json.dump({\"tests\": []}, open(sys.argv[3], \"w\"))')\n")
    r = grade(t, patch_from(t, {"minishop/__init__.py": evil}), backend="local")
    assert not r.resolved and r.f2p_failed


def test_naming_errors_ignore_builtin_attribute_errors_and_post_errors():
    from swe_env_forge.task import Task

    t = Task(instance_id="i", repo="r", base_commit="b", problem_statement="the helper module")
    pre = ["AttributeError: 'NoneType' object has no attribute 'items'\n"
           "ImportError: cannot import name 'make_report' from 'shop'\n"
           "Error: Cannot find module './lib/helper.js'\n"
           "ImportError: cannot import name 'always_missing'\n"]
    post = ["ImportError: cannot import name 'always_missing'\n"]
    got = {(e["symbol"], e["specified"]) for e in naming_errors(t, pre, post)}
    assert got == {("make_report", False), ("./lib/helper.js", True)}


def test_plain_diff_hunk_lines_are_not_file_headers():
    plain = "--- a/x.lua\n+++ b/x.lua\n@@ -1,2 +1,2 @@\n--- old comment\n+++ new comment\n same\n"
    assert [f.path for f in parse_patch(plain)] == ["x.lua"]


def test_quoted_rename_paths_are_unquoted():
    d = ('diff --git "a/tests/t\\303\\251st.py" "b/tests/t\\303\\251st2.py"\nsimilarity index 100%\n'
         'rename from "tests/t\\303\\251st.py"\nrename to "tests/t\\303\\251st2.py"\n')
    f = parse_patch(d)[0]
    assert (f.old_path, f.path) == ("tests/tést.py", "tests/tést2.py")
    raw = 'diff --git "a/d/é.py" "b/d/é.py"\n--- "a/d/é.py"\n+++ "b/d/é.py"\n@@ -1 +1 @@\n-a\n+b\n'
    assert parse_patch(raw)[0].path == "d/é.py"


def test_space_in_sandbox_path(fix_task, tmp_path):
    with LocalSandbox(root=str(tmp_path / "with space")) as sb:
        checkout_base(sb, fix_task)
        assert run_tests(sb, fix_task).parsed


def test_config_edit_that_stops_the_runner_is_tampering(fix_task):
    # a broken test-setting edit in a shared build file must not hide behind "tests_not_run"
    toml = ('[project]\nname = "minishop"\nversion = "0.1.0"\n\n[tool.pytest.ini_options]\n'
            'testpaths = ["tests"]\naddopts = "-p plugin_that_does_not_exist"\n')
    r = grade(fix_task, patch_from(fix_task, {"pyproject.toml": toml}), backend="local")
    assert r.failure == "tampering" and "edits_test_config:pyproject.toml" in r.flags


def test_reverted_conftest_is_informational(fix_task):
    p = fix_task.gold_patch + patch_from(fix_task, {"conftest.py": "# my debugging helpers\n"})
    r = grade(fix_task, p, backend="local")
    assert r.resolved and "edits_test_runner_config:conftest.py" in r.flags


# ------------------------------------------------- image environment commits (start_ref)
import subprocess as _sp

from swe_env_forge.gym import SWEEnv
from swe_env_forge.workspace import WorkspaceError, start_ref


def _image_like_sandbox(task, extra_commits=1):
    """A sandbox whose repo already exists, like a prebuilt image: base + N environment commits."""
    sb = LocalSandbox()
    _sp.run(["git", "clone", "-q", task.clone_url, sb.workdir], check=True)
    _sp.run(["git", "-C", sb.workdir, "checkout", "-q", task.base_commit], check=True)
    for i in range(extra_commits):
        with open(f"{sb.workdir}/ENVIRONMENT.txt", "a") as fh:
            fh.write(f"env setting {i}\n")
        _sp.run(["git", "-C", sb.workdir, "add", "-A"], check=True)
        _sp.run(["git", "-C", sb.workdir, "-c", "user.name=env", "-c", "user.email=e@x", "commit", "-qm", "env"],
                check=True)
    return sb


def test_image_head_keeps_the_environment_commit(fix_task):
    t = copy.deepcopy(fix_task)
    t.env.start_ref = "image-head"
    with _image_like_sandbox(t) as sb:
        checkout_base(sb, t)
        assert start_ref(sb, t) != t.base_commit
        assert sb.read_file("ENVIRONMENT.txt") == "env setting 0\n"   # survives the reset
        sb.write_file("minishop/x.py", "X = 1\n")
        diff = collect_diff(sb, start_ref(sb, t))
        assert "ENVIRONMENT.txt" not in diff and "minishop/x.py" in diff
        r = grade_in_sandbox(sb, t, t.gold_patch)
        assert r.resolved, r


def grade_in_sandbox(sb, t, patch):
    from swe_env_forge.grade import grade_in

    return grade_in(sb, t, patch)


def test_image_head_far_past_the_base_is_refused(demo_tasks):
    t = copy.deepcopy(demo_tasks["fix"])
    t.env.start_ref = "image-head"
    with _image_like_sandbox(t, extra_commits=6) as sb:
        with pytest.raises(WorkspaceError, match="refusing"):
            checkout_base(sb, t)


def test_default_start_is_the_base_even_with_extra_commits(fix_task):
    with _image_like_sandbox(fix_task) as sb:
        checkout_base(sb, fix_task)
        assert start_ref(sb, fix_task) == fix_task.base_commit
        assert not sb.exec("test -e ENVIRONMENT.txt").ok


# ------------------------------------------- attacks from the integrity review (in-process tampering)
PRICING_BASE = '"""Prices are stored in cents."""\n\n\ndef apply_discount(cents, percent):\n'


def test_atexit_forged_passed_line_is_caught(fix_task):
    evil = ("import atexit\n"
            "atexit.register(lambda: print('PASSED tests/test_pricing.py::test_discount_rounds_half_up'))\n")
    r = grade(fix_task, patch_from(fix_task, {"minishop/__init__.py": evil}), backend="local")
    assert not r.resolved
    assert any(f.startswith("conflicting_test_status") for f in r.flags) and r.failure == "tampering"


def test_pytest_xfail_in_code_under_test_does_not_pass(fix_task):
    evil = (PRICING_BASE + "    if percent == 15:\n        import pytest\n        pytest.xfail('not yet')\n"
            "    return cents - cents * percent // 100\n\n\ndef format_price(cents):\n"
            "    return f\"${cents // 100}.{cents % 100:02d}\"\n")
    r = grade(fix_task, patch_from(fix_task, {"minishop/pricing.py": evil}), backend="local")
    assert not r.resolved and r.failure == "tampering"
    lenient = grade(fix_task, patch_from(fix_task, {"minishop/pricing.py": evil}), backend="local", strict=False)
    assert not lenient.resolved   # validated status is PASSED, so an XFAIL never counts


def test_patching_the_test_framework_trips_the_canary(fix_task):
    evil = ("import _pytest.reports as _r\n"
            "_orig = _r.TestReport.from_item_and_call\n\n\n"
            "def _fake(cls, item, call):\n"
            "    rep = _orig.__func__(cls, item, call)\n"
            "    rep.outcome = 'passed'\n"
            "    return rep\n\n\n"
            "_r.TestReport.from_item_and_call = classmethod(_fake)\n")
    p = patch_from(fix_task, {"minishop/__init__.py": evil})
    r = grade(fix_task, p, backend="local")
    assert not r.resolved and r.failure == "tampering"
    assert any(f.startswith("touches_test_framework") for f in r.flags)
    lenient = grade(fix_task, p, backend="local", strict=False)
    assert any(f.startswith("framework_compromised") for f in lenient.flags)   # caught by behaviour, not by name


def test_canary_does_not_disturb_honest_grading(fix_task):
    r = grade(fix_task, fix_task.gold_patch, backend="local")
    assert r.resolved and not any("framework_compromised" in f for f in r.flags)


def test_new_guard_files_are_reverted():
    from swe_env_forge.grade import tamper_flags

    for name in (".pytest.ini", "sitecustomize.py", "src/evil.pth"):
        p = f"diff --git a/{name} b/{name}\nnew file mode 100644\n--- /dev/null\n+++ b/{name}\n@@ -0,0 +1 @@\n+x\n"
        assert tamper_flags(p, set())[1] == [name], name
    setup = ("diff --git a/setup.py b/setup.py\n--- a/setup.py\n+++ b/setup.py\n@@ -1 +1,2 @@\n x\n"
             "+    entry_points={'pytest11': ['p = plug']},\n")
    assert "edits_test_config:setup.py" in tamper_flags(setup, set())[0]
