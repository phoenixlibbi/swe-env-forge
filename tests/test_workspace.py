import subprocess

from swe_env_forge.sandbox import LocalSandbox
from swe_env_forge.workspace import (apply_patch, checkout_base, collect_diff, prepare, revert_files, run_tests,
                                   strip_future_history)


def test_checkout_and_run_tests(demo_tasks):
    t = demo_tasks["fix"]
    with LocalSandbox() as sb:
        checkout_base(sb, t)
        assert apply_patch(sb, t.test_patch, "tests", reset_to=t.base_commit).ok
        run = run_tests(sb, t)
        assert run.statuses["tests/test_pricing.py::test_discount_rounds_half_up"] == "FAILED"
        assert run.statuses["tests/test_pricing.py::test_no_discount"] == "PASSED"


def test_strip_history_hides_the_fix(demo_tasks):
    t = demo_tasks["fix"]
    instance = t.metadata["instance_commit"]
    with LocalSandbox() as sb:
        prepare(sb, t, for_agent=True)
        res = sb.exec("git log --all --format=%H")
        assert instance not in res.output and t.base_commit in res.output
        assert not sb.exec(f"git cat-file -e {instance}").ok
        assert sb.exec("git remote").output.strip() == ""


def test_strip_history_rejects_nothing_on_plain_repo(demo_tasks):
    t = demo_tasks["bulk"]
    with LocalSandbox() as sb:
        checkout_base(sb, t)
        assert "history-stripped" in strip_future_history(sb, t.base_commit).output


def test_collect_diff_includes_new_files(demo_tasks):
    t = demo_tasks["fix"]
    with LocalSandbox() as sb:
        checkout_base(sb, t)
        sb.write_file("minishop/new.py", "X = 1\n")
        sb.write_file("README.md", "changed\n")
        diff = collect_diff(sb, t.base_commit)
        assert "b/minishop/new.py" in diff and "b/README.md" in diff
        # the tree is left as the agent made it
        assert sb.read_file("minishop/new.py") == "X = 1\n"


def test_apply_patch_fallbacks(demo_tasks):
    t = demo_tasks["fix"]
    with LocalSandbox() as sb:
        checkout_base(sb, t)
        # a patch whose context is slightly off: plain git apply fails, a fallback succeeds
        lines = t.gold_patch.splitlines(keepends=True)
        first_ctx = next(i for i, ln in enumerate(lines) if ln.startswith(" ") and lines[i - 1].startswith("@@"))
        lines[first_ctx] = " # this context line does not exist in the file\n"
        drifted = "".join(lines)
        res = apply_patch(sb, drifted, "drifted", reset_to=t.base_commit)
        assert res.ok and res.method == "patch --fuzz", res.output
        assert "+ 50) // 100" in sb.read_file("minishop/pricing.py")
        assert not apply_patch(sb, "diff --git a/nope.py b/nope.py\n--- a/nope.py\n+++ b/nope.py\n@@ -1 +1 @@\n-a\n+b\n",
                               "bad", reset_to=t.base_commit).ok
        assert apply_patch(sb, "", "empty").ok


def test_revert_files(demo_tasks):
    t = demo_tasks["fix"]
    with LocalSandbox() as sb:
        checkout_base(sb, t)
        original = sb.read_file("minishop/pricing.py")
        sb.write_file("minishop/pricing.py", "broken\n")
        sb.write_file("minishop/extra.py", "new\n")
        revert_files(sb, t.base_commit, ["minishop/pricing.py", "minishop/extra.py"])
        assert sb.read_file("minishop/pricing.py") == original
        assert not sb.exec("test -e minishop/extra.py").ok


def test_local_sandbox_timeout():
    with LocalSandbox() as sb:
        res = sb.exec("sleep 5", timeout=0.5)
        assert res.timed_out and not res.ok
