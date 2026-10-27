import json

import pytest

from swe_env_kit.task import EnvSpec, Task, load_tasks, read_jsonl, save_tasks, write_jsonl


def make(**kw):
    base = dict(instance_id="o__r-1", repo="o/r", base_commit="abc")
    base.update(kw)
    return Task.from_dict(base)


def test_required_fields():
    with pytest.raises(ValueError, match="base_commit"):
        Task.from_dict({"instance_id": "x", "repo": "o/r"})


def test_test_lists_accept_json_strings_and_python_literals():
    t = make(fail_to_pass='["a", "b"]', pass_to_pass="['c']")
    assert t.fail_to_pass == ["a", "b"]
    assert t.pass_to_pass == ["c"]
    assert make(fail_to_pass=None).fail_to_pass == []


def test_unknown_top_level_keys_are_kept_in_metadata():
    t = make(FOO=1)
    assert t.metadata["extra"] == {"FOO": 1}


def test_unknown_env_key_is_an_error():
    with pytest.raises(ValueError, match="bogus"):
        make(env={"bogus": 1})


def test_env_string_lists_are_normalised():
    t = make(env={"setup": "source x", "test_files": ""})
    assert t.env.setup == ["source x"]
    assert t.env.test_files == []


def test_roundtrip(tmp_path):
    t = make(env={"image": "img", "test_command": "pytest {test_files}"}, metadata={"k": 1})
    path = tmp_path / "t.jsonl"
    save_tasks(path, [t, t])
    loaded = load_tasks(path)
    assert loaded == [t, t]
    assert loaded[0].env == EnvSpec(image="img", test_command="pytest {test_files}")


def test_agent_view_hides_answers():
    t = make(gold_patch="SECRET", test_patch="SECRET", hints="h", fail_to_pass=["x"])
    view = t.agent_view()
    assert "SECRET" not in json.dumps(view)
    assert "hints" not in view
    assert t.agent_view(include_hints=True)["hints"] == "h"


def test_clone_url():
    assert make().clone_url == "https://github.com/o/r.git"
    assert make(repo_url="/tmp/x").clone_url == "/tmp/x"


def test_read_json_list_and_object(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps([{"a": 1}, {"a": 2}]))
    (tmp_path / "b.json").write_text(json.dumps({"a": 3}))
    assert [r["a"] for r in read_jsonl(tmp_path / "a.json")] == [1, 2]
    assert [r["a"] for r in read_jsonl(tmp_path / "b.json")] == [3]


def test_bad_jsonl_line_reports_location(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text('{"a": 1}\n{oops}\n')
    with pytest.raises(ValueError, match="x.jsonl:2"):
        list(read_jsonl(p))


def test_write_jsonl_counts(tmp_path):
    assert write_jsonl(tmp_path / "d" / "o.jsonl", [{"a": 1}, {"b": 2}]) == 2
