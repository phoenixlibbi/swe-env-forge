import json

import pytest

from swe_env_kit.cli import main
from swe_env_kit.task import load_tasks, read_jsonl


@pytest.fixture(scope="module")
def pipeline(tmp_path_factory):
    """demo -> mine -> spec -> validate, shared by the CLI tests."""
    d = tmp_path_factory.mktemp("cli")
    repo = d / "minishop"
    assert main(["demo", str(repo)]) == 0
    assert main(["mine", str(repo), "-o", str(d / "mined.jsonl"), "--repo", "demo/minishop"]) == 0
    assert main(["spec", str(d / "mined.jsonl"), "-o", str(d / "spec.jsonl"), "--report", str(d / "spec-report.jsonl")]) == 0
    from tests.conftest import FLAKY_COUNTER

    FLAKY_COUNTER.unlink(missing_ok=True)
    assert main(["validate", str(d / "spec.jsonl"), "-o", str(d / "tasks.jsonl"), "--backend", "local",
                 "--workers", "2", "--report", str(d / "validation.jsonl")]) == 0
    return d


def test_mine_and_validate_outputs(pipeline):
    mined = load_tasks(pipeline / "mined.jsonl")
    assert len(mined) == 4
    tasks = load_tasks(pipeline / "tasks.jsonl")
    assert len(tasks) == 4 and all(t.fail_to_pass for t in tasks)
    rep = list(read_jsonl(pipeline / "validation.jsonl"))
    assert [r["verdict"] for r in rep] == ["valid"] * 4
    assert any(r["flaky"] for r in rep)
    # spec gave the feature task an interface, so its naming leak is resolved
    feature = next(t for t in tasks if "low-stock" in t.problem_statement)
    assert "low_stock_items" in feature.interface
    assert not any(w == "naming_leak" for r in rep for w in r["warnings"] if r["instance_id"] == feature.instance_id)


def test_grade_gold_and_nop(pipeline, capsys):
    capsys.readouterr()
    assert main(["grade", str(pipeline / "tasks.jsonl"), "--gold", "--backend", "local", "-o", str(pipeline / "g.jsonl")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["resolved"] == 4 and out["mean_dense"] == 1.0
    assert main(["grade", str(pipeline / "tasks.jsonl"), "--nop", "--backend", "local", "-o", str(pipeline / "n.jsonl")]) == 0
    assert json.loads(capsys.readouterr().out)["resolved"] == 0


def test_run_and_report(pipeline, capsys):
    runs = pipeline / "runs"
    assert main(["run", str(pipeline / "tasks.jsonl"), "--agent", "oracle", "--backend", "local",
                 "-o", str(runs / "oracle"), "--checkpoint-every", "1", "--grade-checkpoints"]) == 0
    assert main(["run", str(pipeline / "tasks.jsonl"), "--agent", "nop", "--backend", "local",
                 "-o", str(runs / "nop")]) == 0
    capsys.readouterr()
    md_path = pipeline / "report.md"
    assert main(["report", str(runs / "oracle"), str(runs / "nop"), "--tasks", str(pipeline / "tasks.jsonl"),
                 "--md", str(md_path), "--json", str(pipeline / "report.json")]) == 0
    md = md_path.read_text()
    assert "| resolved % | 100.0 | 0.0 |" in md
    assert "empty_patch" in md
    preds = list(read_jsonl(runs / "oracle" / "predictions.jsonl"))
    assert len(preds) == 4 and all(p["model_patch"] for p in preds)
    # predictions written by `run` can be graded again by `grade`
    capsys.readouterr()
    assert main(["grade", str(pipeline / "tasks.jsonl"), str(runs / "oracle" / "predictions.jsonl"),
                 "--backend", "local", "-o", str(pipeline / "regrade.jsonl")]) == 0
    assert json.loads(capsys.readouterr().out)["resolved"] == 4


def test_replay_agent_from_directory(pipeline):
    tasks = load_tasks(pipeline / "tasks.jsonl")
    fix = next(t for t in tasks if t.problem_statement.startswith("Fix rounding"))
    rdir = pipeline / "replays"
    rdir.mkdir()
    (rdir / f"{fix.instance_id}.txt").write_text(
        "sed -i.bak 's|return cents - cents \\* percent // 100|return cents - (cents * percent + 50) // 100|' "
        "minishop/pricing.py\nrm minishop/pricing.py.bak\n")
    assert main(["run", str(pipeline / "tasks.jsonl"), "--agent", f"replay:{rdir}", "--ids", fix.instance_id,
                 "--backend", "local", "-o", str(pipeline / "runs" / "replay")]) == 0
    ep = json.loads((pipeline / "runs" / "replay" / fix.instance_id / "episode.json").read_text())
    assert ep["grade"]["resolved"] and ep["turns"] == 2


def test_stats_convert_localize(pipeline, capsys):
    capsys.readouterr()
    assert main(["stats", str(pipeline / "mined.jsonl"), "--min-lines", "10"]) == 0
    s = json.loads(capsys.readouterr().out)
    assert s["tasks"] == 4 and s["pro_eligible"]["count"] == 1
    assert main(["convert", str(pipeline / "tasks.jsonl"), "--to", "swebench", "-o", str(pipeline / "sb.jsonl")]) == 0
    rows = list(read_jsonl(pipeline / "sb.jsonl"))
    assert "FAIL_TO_PASS" in rows[0]
    assert main(["convert", str(pipeline / "sb.jsonl"), "-o", str(pipeline / "back.jsonl")]) == 0
    assert main(["localize", str(pipeline / "tasks.jsonl"), "--budgets", "200,100000"]) == 0
    loc = json.loads(capsys.readouterr().out)
    assert loc["tasks"] == 3 and loc["budgets"]["100000"]["all"] == 100.0


def test_spec_check_only_exit_code(pipeline, tmp_path):
    tasks = load_tasks(pipeline / "tasks.jsonl")
    t = tasks[0]
    gold_line = next(ln[1:] for ln in t.gold_patch.splitlines() if ln.startswith("+") and len(ln) > 35
                     and not ln.startswith("+++"))
    t.problem_statement += "\nJust write: " + gold_line.strip()
    from swe_env_kit.task import save_tasks

    save_tasks(tmp_path / "leaky.jsonl", [t])
    assert main(["spec", str(tmp_path / "leaky.jsonl"), "--check-only"]) == 1
    assert main(["spec", str(pipeline / "mined.jsonl"), "--check-only"]) == 0


def test_import_traj(pipeline, tmp_path):
    tasks = load_tasks(pipeline / "tasks.jsonl")
    t = tasks[0]
    traj = {"trajectory": [{"action": "ls", "observation": "x", "thought": "", "execution_time": 0.1}],
            "info": {"submission": t.gold_patch, "exit_status": "submitted", "model_stats": {"instance_cost": 0.3}}}
    (tmp_path / f"{t.instance_id}.traj").write_text(json.dumps(traj))
    assert main(["import-traj", str(tmp_path), "--tasks", str(pipeline / "tasks.jsonl"), "--grade", "--backend", "local",
                 "-o", str(tmp_path / "imported")]) == 0
    ep = json.loads((tmp_path / "imported" / t.instance_id / "episode.json").read_text())
    assert ep["grade"]["resolved"] and ep["cost"] == 0.3


def test_errors_are_reported(tmp_path, capsys):
    assert main(["stats", str(tmp_path / "missing.jsonl")]) == 1
    assert main(["grade", str(tmp_path / "missing.jsonl")]) == 1
