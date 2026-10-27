import copy
import json

import pytest

from swe_env_kit.agents import LLMAgent, NopAgent, OracleAgent, ReplayAgent, load_agent_class, parse_reply
from swe_env_kit.gym import Budget, SWEEnv, run_batch, run_episode, truncate
from swe_env_kit.llm import Usage
from swe_env_kit.trajectory import Episode, from_swe_agent, load_episodes
from swe_env_kit.validate import apply_validation, validate


@pytest.fixture(scope="module")
def fix_task(demo_tasks):
    t = copy.deepcopy(demo_tasks["fix"])
    return apply_validation(t, validate(t, backend="local", runs=1))


FIX_CMD = "sed -i.bak 's|return cents - cents \\* percent // 100|return cents - (cents * percent + 50) // 100|' minishop/pricing.py && rm minishop/pricing.py.bak"


def test_oracle_and_nop(fix_task, tmp_path):
    ep = run_episode(fix_task, OracleAgent(fix_task), "local", out_dir=tmp_path)
    assert ep.exit_reason == "submitted" and ep.grade["resolved"] and ep.grade["dense"] == 1.0
    nop = run_episode(fix_task, NopAgent(), "local")
    assert nop.turns == 0 and not nop.grade["resolved"] and nop.grade["failure"] == "empty_patch"
    loaded = Episode.load(tmp_path / fix_task.instance_id)
    assert loaded.grade["resolved"] and len(loaded.steps) == 2


def test_replay_fix_and_cwd_persistence(fix_task):
    agent = ReplayAgent(["cd minishop", "pwd", "cd ..", FIX_CMD])
    ep = run_episode(fix_task, agent, "local")
    assert ep.steps[1].output.strip().endswith("/repo/minishop")
    assert ep.grade["resolved"], ep.grade


def test_history_is_hidden_from_agents(fix_task):
    ep = run_episode(fix_task, ReplayAgent(["git log --all --format=%H"]), "local", do_grade=False)
    assert fix_task.metadata["instance_commit"] not in ep.steps[0].output
    assert fix_task.base_commit in ep.steps[0].output


def test_turn_budget_and_checkpoint_grades(fix_task):
    agent = ReplayAgent(["true", FIX_CMD, "true", "true", "true"])
    ep = run_episode(fix_task, agent, "local", Budget(max_turns=4), checkpoint_every=1, grade_checkpoints=True)
    assert ep.exit_reason == "max_turns" and ep.turns == 4
    assert [c.turn for c in ep.checkpoints] == [1, 2, 3, 4]
    assert [c.grade["resolved"] for c in ep.checkpoints] == [False, True, True, True]


def test_cost_budget(fix_task):
    class Spender:
        name = "spender"

        def reset(self, obs):
            pass

        def act(self, last):
            return {"type": "bash", "command": "true", "cost": 0.75}

    ep = run_episode(fix_task, Spender(), "local", Budget(max_cost=2.0), do_grade=False)
    assert ep.exit_reason == "max_cost" and ep.turns == 3 and ep.cost == 2.25


def test_agent_crash_is_recorded(fix_task):
    class Crash:
        def reset(self, obs):
            pass

        def act(self, last):
            raise RuntimeError("boom")

    ep = run_episode(fix_task, Crash(), "local")
    assert ep.exit_reason == "agent_error" and "boom" in ep.error and ep.grade is not None


def test_env_steps_and_bad_actions(fix_task):
    with SWEEnv(fix_task, "local", Budget(max_output_chars=200)) as env:
        obs = env.reset()
        assert "SECRET" not in json.dumps(obs) and "gold_patch" not in obs["task"]
        out, done, _ = env.step({"type": "bash", "command": "python -c \"print('x' * 1000)\""})
        assert "characters truncated" in out and not done
        out, _, _ = env.step({"type": "nope"})
        assert "unknown action" in out
        out, _, _ = env.step({"type": "write_file", "path": "notes.txt", "content": "hi"})
        assert out == "wrote notes.txt"
        _, done, info = env.step({"type": "submit"})
        assert done and info["exit_reason"] == "submitted" and "notes.txt" in env.final_patch


def test_run_batch_writes_predictions(fix_task, tmp_path):
    eps = run_batch([fix_task], lambda t: OracleAgent(t), "local", out_dir=tmp_path, model_name="oracle")
    assert eps[0].grade["resolved"]
    pred = json.loads((tmp_path / "predictions.jsonl").read_text().splitlines()[0])
    assert pred["model_name_or_path"] == "oracle" and pred["model_patch"] == eps[0].patch
    assert len(load_episodes(tmp_path)) == 1
    assert run_batch([fix_task], lambda t: OracleAgent(t), "local", out_dir=tmp_path) == []  # resumable


def test_parse_reply():
    assert parse_reply("THOUGHT: look\n```bash\nls -la\n```") == {"type": "bash", "command": "ls -la", "thought": "look"}
    assert parse_reply("THOUGHT: done\nSUBMIT")["type"] == "submit"
    assert parse_reply("just chatting")["type"] == "format_error"
    two = parse_reply("```bash\nfirst\n```\nthen\n```bash\nsecond\n```")
    assert two["command"] == "second"


class ScriptedClient:
    model = "scripted"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def chat(self, messages):
        self.seen.append([m["content"] for m in messages])
        return self.replies.pop(0), Usage(prompt_tokens=10, completion_tokens=5, cost=0.01, calls=1)


def test_llm_agent_end_to_end(fix_task):
    client = ScriptedClient(["THOUGHT: inspect\n```bash\ncat minishop/pricing.py\n```",
                             "I forgot the format",
                             f"THOUGHT: fix it\n```bash\n{FIX_CMD}\n```",
                             "THOUGHT: done\nSUBMIT"])
    ep = run_episode(fix_task, LLMAgent(fix_task, client, mode="text"), "local")
    assert ep.grade["resolved"] and ep.exit_reason == "submitted"
    assert ep.turns == 2 and abs(ep.cost - 0.04) < 1e-9  # the malformed reply costs money but not a turn
    assert "<problem_statement>" in client.seen[0][1]
    assert any("def apply_discount" in c for c in client.seen[1])  # observation was fed back


def test_load_agent_class():
    assert load_agent_class("swe_env_kit.agents:NopAgent") is NopAgent
    with pytest.raises(ValueError):
        load_agent_class("nocolon")


def test_import_swe_agent_trajectory(tmp_path):
    traj = {"trajectory": [{"action": "ls", "observation": "a.py", "thought": "look", "execution_time": 0.5},
                           {"action": "submit", "observation": "", "thought": "", "execution_time": 0.1}],
            "info": {"submission": "diff --git a/a.py b/a.py\n", "exit_status": "submitted",
                     "model_stats": {"instance_cost": 0.42, "api_calls": 2}, "swe_agent_version": "1.1.0"}}
    p = tmp_path / "o__r-1.traj"
    p.write_text(json.dumps(traj))
    ep = from_swe_agent(p)
    assert ep.instance_id == "o__r-1" and ep.turns == 2 and ep.cost == 0.42
    assert ep.exit_reason == "submitted" and ep.patch.startswith("diff --git")
    assert ep.agent == "swe-agent:1.1.0"
    traj["info"]["exit_status"] = "exit_cost"
    p.write_text(json.dumps(traj))
    assert from_swe_agent(p).exit_reason == "max_cost"


def test_truncate():
    assert truncate("abc", 10) == "abc"
    t = truncate("x" * 100, 30)
    assert "truncated" in t and t.startswith("x" * 20)
