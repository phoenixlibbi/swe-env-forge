from swe_env_kit.failure import classify, distribution, llm_judge, stuck_in_loop
from swe_env_kit.report import budget_curves, build_report, solved_at, to_markdown
from swe_env_kit.task import Task
from swe_env_kit.trajectory import Checkpoint, Episode, Step

GOLD = "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-a\n+b\n"
OTHER = "diff --git a/src/b.py b/src/b.py\n--- a/src/b.py\n+++ b/src/b.py\n@@ -1 +1 @@\n-a\n+b\n"


def task(iid="t1", lang="python", **kw):
    return Task(instance_id=iid, repo="o/r", base_commit="b", gold_patch=GOLD, language=lang, **kw)


def bash_steps(cmds, cost=0.1, exit_code=0):
    return [Step(turn=i + 1, action={"type": "bash", "command": c}, cost=cost, exit_code=exit_code)
            for i, c in enumerate(cmds)]


def ep(iid="t1", exit_reason="submitted", grade=None, patch=GOLD, steps=None, **kw):
    steps = steps if steps is not None else bash_steps(["ls", "edit"])
    return Episode(instance_id=iid, agent="a", exit_reason=exit_reason, turns=len(steps),
                   cost=round(sum(s.cost for s in steps), 6), patch=patch, steps=steps,
                   grade=grade or {"resolved": False, "dense": 0.0, "failure": "wrong_solution", "flags": []}, **kw)


def test_classify_submitted_buckets():
    t = task()
    assert classify(ep(grade={"resolved": True}), t) == ""
    assert classify(ep(patch="", grade={"failure": "empty_patch"}), t) == "empty_patch"
    assert classify(ep(grade={"failure": "patch_apply_error"}), t) == "malformed_patch"
    assert classify(ep(grade={"failure": "wrong_solution", "flags": ["adds_skip_marker:x"]}), t) == "instruction_following"
    assert classify(ep(grade={"failure": "wrong_solution", "test_log_tail": "E   SyntaxError: invalid syntax"}), t) == "syntax_error"
    assert classify(ep(patch=OTHER), t) == "incorrect_file"
    assert classify(ep(grade={"failure": "regression"}), t) == "regression"
    assert classify(ep(grade={"failure": "partial"}), t) == "edge_case"
    assert classify(ep(), t) == "wrong_solution"


def test_classify_not_submitted_buckets():
    assert classify(ep(exit_reason="context_overflow")) == "long_context"
    assert classify(ep(exit_reason="max_turns", steps=bash_steps(["ls", "pytest", "pytest", "pytest"]))) == "stuck_in_loop"
    assert classify(ep(exit_reason="max_turns", steps=bash_steps(["x", "y", "z"], exit_code=1))) == "tool_use"
    reads = bash_steps(["cat a.py", "grep -rn foo .", "sed -n 1,50p b.py", "find . -name '*.py'", "ls src"])
    assert classify(ep(exit_reason="max_turns", patch="", steps=reads)) == "endless_reading"
    assert classify(ep(exit_reason="max_cost", steps=bash_steps(["a", "b", "c", "d"]))) == "budget_exhausted"
    assert classify(ep(exit_reason="env_error")) == "harness_error"


def test_stuck_in_loop_cycles():
    assert stuck_in_loop(["a", "b", "a", "b", "a", "b"])
    assert not stuck_in_loop(["a", "b", "c", "a", "b", "d"])


def test_distribution():
    d = distribution([ep(), ep(exit_reason="max_turns", steps=bash_steps(["a", "a", "a"])), ep(grade={"resolved": True})])
    assert d["failures"] == 2
    assert d["submitted"]["buckets"]["wrong_solution"]["count"] == 1
    assert d["not_submitted"]["buckets"]["stuck_in_loop"]["count"] == 1


def test_solved_at_uses_checkpoints():
    e = ep(grade={"resolved": True}, steps=bash_steps(["a", "b", "c", "d"], cost=0.5))
    e.checkpoints = [Checkpoint(1, "", {"resolved": False}), Checkpoint(2, "", {"resolved": True}),
                     Checkpoint(4, "", {"resolved": True})]
    assert solved_at(e) == (2, 1.0)
    e.checkpoints = []
    assert solved_at(e) == (4, 2.0)
    assert solved_at(ep()) is None


def test_budget_curves_explain_capped_vs_uncapped_scores():
    fast = ep("a", grade={"resolved": True}, steps=bash_steps(["x"] * 5, cost=0.2))      # $1.0, 5 turns
    slow = ep("b", grade={"resolved": True}, steps=bash_steps(["x"] * 40, cost=0.1))     # $4.0, 40 turns
    fail = ep("c", steps=bash_steps(["x"] * 3))
    bc = budget_curves([fast, slow, fail], turn_caps=(5, 50), cost_caps=(2.0,))
    assert bc["by_turns"] == {"5": 33.3, "50": 66.7}
    assert bc["by_cost"] == {"2": 33.3}
    assert bc["uncapped"] == 66.7


def test_build_report_and_markdown():
    tasks = [task("a", "python", metadata={"issue_categories": ["back_end_knowledge"]}), task("b", "go")]
    eps = [ep("a", grade={"resolved": True, "dense": 1.0, "flags": []}),
           ep("b", grade={"resolved": False, "dense": 0.5, "failure": "partial", "flags": ["edits_test_file:x"]})]
    r = build_report(eps, tasks=tasks, name="demo")
    assert r["slices"]["overall"]["all"] == {"n": 2, "resolved_pct": 50.0, "mean_dense": 0.75}
    assert r["slices"]["language"]["go"]["resolved_pct"] == 0.0
    assert r["slices"]["gold_lines"]["1-9"]["n"] == 2
    assert r["slices"]["category"]["back_end_knowledge"]["n"] == 1
    assert r["flags"] == {"edits_test_file": 1}
    md = to_markdown([r, build_report(eps[:1], tasks=tasks, name="other")])
    assert "| resolved % | 50.0 | 100.0 |" in md
    assert "## Budget curves" in md and "## Failure modes" in md and "edge_case" in md


def test_report_from_grades_only():
    r = build_report(grades=[{"instance_id": "a", "resolved": True, "dense": 1.0}], tasks=[task("a")])
    assert r["n"] == 1 and "budget_curves" not in r
    assert "# swe-env-kit report" in to_markdown([r])


def test_llm_judge_parses_bucket():
    class C:
        def chat(self, messages):
            assert "Last 20 turns" in messages[0]["content"]
            return "It kept re-running the same test.\nBUCKET: stuck_in_loop", None

    assert llm_judge(ep(exit_reason="max_turns"), C())["bucket"] == "stuck_in_loop"


def test_ungraded_episodes_are_not_counted_as_failures():
    graded = ep("a", grade={"resolved": True, "dense": 1.0, "flags": []})
    ungraded = ep("b", grade=None)
    ungraded.grade = None
    r = build_report([graded, ungraded], name="mix")
    assert r["n"] == 1 and r["ungraded"] == 1 and r["episodes"] == 2
    assert r["slices"]["overall"]["all"]["resolved_pct"] == 100.0
    assert r["failure_modes"]["failures"] == 0


def test_repeated_attempts_are_kept(tmp_path):
    first, second = ep("t"), ep("t")
    first.save(tmp_path, unique=True)
    second.save(tmp_path, unique=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["t", "t@2"]
    from swe_env_kit.trajectory import load_episodes

    eps = load_episodes(tmp_path)
    assert len(eps) == 2 and eps[1].metadata["attempt"] == 2
