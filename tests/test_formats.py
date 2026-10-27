import json

import pytest

from swe_env_kit import hub
from swe_env_kit.formats import (detect_format, from_generic, load_any, load_predictions, to_pro, to_swebench,
                                 to_task)
from swe_env_kit.presets import parse_swebench_eval_script, render_test_files, swebench_image

EVAL_SCRIPT = """#!/bin/bash
set -uxo pipefail
source /opt/miniconda3/bin/activate
conda activate testbed
cd /testbed
git config --global --add safe.directory /testbed
cd /testbed
git status
git show
git -c core.fileMode=false diff abc123
source /opt/miniconda3/bin/activate
conda activate testbed
python -m pip install -e .[test] --verbose
git checkout abc123 pkg/tests/test_a.py
git apply -v - <<'EOF_1'
diff --git a/pkg/tests/test_a.py b/pkg/tests/test_a.py
EOF_1
: '>>>>> Start Test Output'
pytest -rA pkg/tests/test_a.py
: '>>>>> End Test Output'
git checkout abc123 pkg/tests/test_a.py
"""

SWEBENCH_ROW = {
    "repo": "acme/widgets", "instance_id": "acme__widgets-1", "base_commit": "abc123",
    "patch": "diff --git a/pkg/a.py b/pkg/a.py\n", "test_patch": "diff --git a/pkg/tests/test_a.py b/pkg/tests/test_a.py\n",
    "problem_statement": "broken", "hints_text": "look at a.py", "created_at": "2023-01-01",
    "version": "1.0", "FAIL_TO_PASS": '["pkg/tests/test_a.py::test_x"]', "PASS_TO_PASS": "[]",
    "environment_setup_commit": "def", "image": "swebench/sweb.eval.x86_64.acme_1776_widgets-1:latest",
    "log_parser": "parse_log_astropy", "eval_script": EVAL_SCRIPT,
}

PRO_ROW = {
    "repo": "acme/app", "instance_id": "instance_acme__app-1-vnan", "base_commit": "b1",
    "patch": "p", "test_patch": "t", "problem_statement": "ps", "requirements": "req", "interface": "iface",
    "repo_language": "js", "fail_to_pass": '["a | b"]', "pass_to_pass": "[]",
    "issue_specificity": '["integration_bug"]', "issue_categories": '["back_end_knowledge"]',
    "before_repo_set_cmd": "git reset --hard b1", "selected_test_files_to_run": '["test/a.js", "test/b.js"]',
    "dockerhub_tag": "acme.app-1", "docker_image": "ghcr.io/x/img:1",
}


def test_detect_format():
    assert detect_format(SWEBENCH_ROW) == "swebench"
    assert detect_format(PRO_ROW) == "swebench-pro"
    assert detect_format({"instance_id": "x"}) == "generic"
    assert detect_format(to_task(SWEBENCH_ROW).to_dict()) == "swekit"


def test_eval_script_parsing():
    pre, cmd = parse_swebench_eval_script(EVAL_SCRIPT, "abc123")
    assert pre == ["python -m pip install -e .[test] --verbose"]
    assert cmd == "pytest -rA pkg/tests/test_a.py"
    assert parse_swebench_eval_script("echo hi", "x") is None


def test_from_swebench_uses_dataset_image_parser_and_script():
    t = to_task(SWEBENCH_ROW)
    assert t.source == "swebench" and t.language == "python"
    assert t.fail_to_pass == ["pkg/tests/test_a.py::test_x"] and t.hints == "look at a.py"
    assert t.env.image == SWEBENCH_ROW["image"] and t.env.workdir == "/testbed"
    assert t.env.log_parser == "pytest-v2" and t.env.test_file_format == "none"
    assert t.env.test_command == "pytest -rA pkg/tests/test_a.py"
    assert t.metadata["version"] == "1.0"


def test_from_swebench_without_eval_script_falls_back_to_presets():
    row = {k: v for k, v in SWEBENCH_ROW.items() if k not in ("eval_script", "image", "log_parser")}
    row.update(repo="django/django", instance_id="django__django-11099")
    t = to_task(row)
    assert t.env.image == "swebench/sweb.eval.x86_64.django_1776_django-11099:latest"
    assert t.env.log_parser == "django" and t.env.test_file_format == "django"
    assert "{test_files}" in t.env.test_command


def test_from_pro():
    t = to_task(PRO_ROW)
    assert t.source == "swebench-pro" and t.language == "javascript"
    assert t.requirements == "req" and t.interface == "iface"
    assert t.env.workdir == "/app" and t.env.test_files == ["test/a.js", "test/b.js"]
    assert t.env.test_file_format == "comma" and t.env.log_parser == "json"
    assert t.env.hidden_paths == ["/tests/test_patch.patch"]
    assert t.metadata["issue_categories"] == ["back_end_knowledge"]
    assert t.env.image == "ghcr.io/x/img:1"


def test_generic_rows_map_aliases():
    t = from_generic({"task_id": "o/r#1", "repo": "o/r", "base_commit": "c", "golden_patch": "", "language": "C#",
                      "FAIL_TO_PASS": "['T.A']", "dockerfile": "FROM x", "test_command": "dotnet test",
                      "merged_time": "2025", "difficulty": "hard"})
    assert t.instance_id == "o/r#1" and t.language == "csharp"
    assert t.fail_to_pass == ["T.A"] and t.env.dockerfile == "FROM x"
    assert t.env.test_file_format == "none" and t.created_at == "2025"
    assert t.metadata == {"difficulty": "hard"}
    with pytest.raises(ValueError):
        from_generic({"repo": "x"})


def test_exports_roundtrip_core_fields():
    t = to_task(SWEBENCH_ROW)
    sb = to_swebench(t)
    assert json.loads(sb["FAIL_TO_PASS"]) == t.fail_to_pass and sb["patch"] == t.gold_patch
    pro = to_pro(to_task(PRO_ROW))
    assert pro["repo_language"] == "js"
    assert json.loads(pro["selected_test_files_to_run"]) == ["test/a.js", "test/b.js"]
    assert to_task(pro).requirements == "req"


def test_load_any_mixed(tmp_path):
    p = tmp_path / "mixed.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in (SWEBENCH_ROW, PRO_ROW)))
    assert [t.source for t in load_any(p)] == ["swebench", "swebench-pro"]


def test_load_predictions_formats(tmp_path):
    a = tmp_path / "preds.jsonl"
    a.write_text(json.dumps({"instance_id": "x", "model_patch": "P", "model_name_or_path": "m"}) + "\n")
    b = tmp_path / "pro.json"
    b.write_text(json.dumps([{"instance_id": "y", "patch": "Q"}]))
    c = tmp_path / "map.json"
    c.write_text(json.dumps({"z": "R"}))
    assert load_predictions(a) == {"x": "P"}
    assert load_predictions(b) == {"y": "Q"}
    assert load_predictions(c) == {"z": "R"}


def test_render_test_files():
    files = ["tests/model_fields/test_integer.py", "tests/a b.py"]
    assert render_test_files(files, "path") == "tests/model_fields/test_integer.py 'tests/a b.py'"
    assert render_test_files(files[:1], "django") == "model_fields.test_integer"
    assert render_test_files(files[:1], "dotted-module") == "tests.model_fields.test_integer"
    assert render_test_files(["a/x_test.go", "a/y_test.go", "z_test.go"], "go-package") == ". ./a"
    assert render_test_files(["a", "b"], "comma") == "a,b"
    assert render_test_files(["a"], "none") == ""


def test_swebench_image_name():
    assert swebench_image("psf__requests-2317") == "swebench/sweb.eval.x86_64.psf_1776_requests-2317:latest"


@pytest.mark.network
def test_live_hf_rows():
    rows = list(hub.iter_hf_rows("swebench-lite", limit=3, page=2))
    assert len(rows) == 3 and all("instance_id" in r for r in rows)
    t = to_task(rows[0])
    assert t.env.image and t.env.test_command


@pytest.mark.network
def test_live_pro_scripts():
    row = next(hub.iter_hf_rows("swebench-pro", limit=1))
    scripts = hub.fetch_pro_scripts(row["instance_id"])
    assert "run_selected_tests" in scripts["run_script"] and "parse_test_output" in scripts["parser_script"]
