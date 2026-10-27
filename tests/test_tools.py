import copy
import json

import pytest

from swe_env_kit.agents import LLMAgent
from swe_env_kit.gym import SWEEnv, run_episode
from swe_env_kit.llm import Usage
from swe_env_kit.sandbox import LocalSandbox
from swe_env_kit.tools import TOOL_SCHEMAS, Toolkit, action_from_tool_call
from swe_env_kit.validate import apply_validation, validate
from swe_env_kit.workspace import checkout_base


@pytest.fixture()
def kit(demo_tasks):
    with LocalSandbox() as sb:
        checkout_base(sb, demo_tasks["fix"])
        yield Toolkit(sb)


def ed(**kw):
    return {"type": "editor", **kw}


def test_view_file_and_range(kit):
    out, code = kit.run(ed(command="view", path="minishop/pricing.py", view_range=[4, 5]))
    assert code == 0 and "     4\tdef apply_discount(cents, percent):" in out and "     6" not in out
    out, code = kit.run(ed(command="view", path="minishop"))
    assert code == 0 and "minishop/cart.py" in out
    assert kit.run(ed(command="view", path="nope.py"))[1] == 1


def test_str_replace_unique_missing_and_ambiguous(kit):
    out, code = kit.run(ed(command="str_replace", path="minishop/pricing.py",
                           old_str="return cents - cents * percent // 100",
                           new_str="return cents - (cents * percent + 50) // 100"))
    assert code == 0 and "+ 50) // 100" in out
    assert "+ 50) // 100" in kit.sb.read_file("minishop/pricing.py")
    assert kit.run(ed(command="str_replace", path="minishop/pricing.py", old_str="not there", new_str="x"))[1] == 1
    out, code = kit.run(ed(command="str_replace", path="minishop/pricing.py", old_str="cents", new_str="c"))
    assert code == 1 and "occurs" in out
    assert kit.run(ed(command="undo_edit", path="minishop/pricing.py"))[1] == 0
    assert "cents * percent // 100" in kit.sb.read_file("minishop/pricing.py")


def test_create_insert_undo(kit):
    assert kit.run(ed(command="create", path="minishop/new.py", file_text="A = 1\n"))[1] == 0
    assert kit.run(ed(command="create", path="minishop/new.py", file_text="x"))[1] == 1
    out, code = kit.run(ed(command="insert", path="minishop/new.py", insert_line=1, new_str="B = 2"))
    assert code == 0 and kit.sb.read_file("minishop/new.py") == "A = 1\nB = 2\n"
    kit.run(ed(command="undo_edit", path="minishop/new.py"))
    kit.run(ed(command="undo_edit", path="minishop/new.py"))
    assert not kit.sb.exec("test -e minishop/new.py").ok


def test_search_and_find_file(kit):
    out, code = kit.run({"type": "search", "pattern": "def apply_discount"})
    assert code == 0 and "minishop/pricing.py:4:" in out
    out, _ = kit.run({"type": "search", "pattern": "apply_.*count", "regex": True, "path": "minishop"})
    assert "minishop/cart.py" in out
    assert "No matches" in kit.run({"type": "search", "pattern": "zzz_not_here"})[0]
    out, _ = kit.run({"type": "find_file", "name": "test_*.py"})
    assert "tests/test_cart.py" in out


def test_paths_outside_the_repo_are_refused(kit):
    out, code = kit.run(ed(command="view", path="../scratch"))
    assert code == 1 and "only works inside the repository" in out
    assert kit.run(ed(command="view", path="/etc/passwd"))[1] == 1


def test_tool_schemas_and_mapping():
    assert {t["function"]["name"] for t in TOOL_SCHEMAS} == {"bash", "str_replace_editor", "search", "find_file", "submit"}
    assert action_from_tool_call("str_replace_editor", {"command": "view", "path": "a"}) == {
        "type": "editor", "command": "view", "path": "a"}
    assert action_from_tool_call("submit", {}) == {"type": "submit"}


@pytest.fixture(scope="module")
def fix_task(demo_tasks):
    t = copy.deepcopy(demo_tasks["fix"])
    return apply_validation(t, validate(t, backend="local", runs=1))


def call(i, name, **args):
    return {"id": f"call_{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class ToolClient:
    model = "tool-model"

    def __init__(self, replies):
        self.replies, self.seen = list(replies), []

    def complete(self, messages, tools=None):
        assert tools is not None
        self.seen.append(json.loads(json.dumps(messages)))
        return self.replies.pop(0), Usage(cost=0.01, calls=1)


def test_tool_calling_agent_solves_the_task(fix_task):
    client = ToolClient([
        {"role": "assistant", "content": "Let me look.", "tool_calls": [call(1, "search", pattern="def apply_discount")]},
        {"role": "assistant", "content": None, "tool_calls": [
            call(2, "str_replace_editor", command="view", path="minishop/pricing.py"),
            call(3, "str_replace_editor", command="str_replace", path="minishop/pricing.py",
                 old_str="return cents - cents * percent // 100",
                 new_str="return cents - (cents * percent + 50) // 100")]},
        {"role": "assistant", "content": "I forgot to call a tool."},
        {"role": "assistant", "content": "Done.", "tool_calls": [call(4, "submit")]},
    ])
    ep = run_episode(fix_task, LLMAgent(fix_task, client, keep_observations=1), "local")
    assert ep.grade["resolved"] and ep.exit_reason == "submitted"
    assert [s.action["type"] for s in ep.steps] == ["search", "editor", "editor"]
    last = client.seen[-1]
    tool_msgs = [m for m in last if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_1", "call_2", "call_3"]
    # only the newest observation is kept in full
    assert tool_msgs[0]["content"].startswith("[collapsed") and tool_msgs[1]["content"].startswith("[collapsed")
    assert not tool_msgs[2]["content"].startswith("[collapsed")
    assert any(m["role"] == "user" and "Call one of the tools" in m["content"] for m in last)


def test_env_reports_tool_errors_without_crashing(fix_task):
    with SWEEnv(fix_task, "local") as env:
        env.reset()
        out, done, _ = env.step({"type": "editor", "command": "fly", "path": "x"})
        assert "unknown editor command" in out and not done
        out, _, _ = env.step({"type": "invalid_tool_arguments"})
        assert "unknown action type" in out
