import copy

from swe_env_forge.cli import main
from swe_env_forge.curate import find_duplicates, redact_task, scan_task, strip_trailers
from swe_env_forge.task import Task, load_tasks, save_tasks

GOLD = ("diff --git a/a/x.py b/a/x.py\n--- a/a/x.py\n+++ b/a/x.py\n@@ -1,3 +1,3 @@\n def f():\n"
        "-    return 1\n+    return 2\n x = 1\n")


def t(iid, gold=None, **kw):
    kw.setdefault("gold_patch", gold if gold is not None else GOLD)
    return Task(instance_id=iid, repo="o/r", base_commit="b", **kw)


def test_exact_and_near_duplicates_and_external_overlap():
    shifted = GOLD.replace("@@ -1,3 +1,3 @@", "@@ -10,3 +10,3 @@").replace("    return 2", "    return   2")
    bigger = GOLD + ("diff --git a/a/y.py b/a/y.py\n--- a/a/y.py\n+++ b/a/y.py\n@@ -1 +1,9 @@\n z\n"
                     + "".join(f"+line{i} = {i}\n" for i in range(8)))
    other = GOLD.replace("return 2", "return 3")
    dups = find_duplicates([t("pr-1"), t("commit-twin", shifted), t("other", other)])
    assert [(d.instance_id, d.duplicate_of, d.kind) for d in dups] == [("commit-twin", "pr-1", "exact")]
    near = find_duplicates([t("a", bigger), t("b", bigger.replace("line7 = 7", "line7 = 70"))], threshold=0.6)
    assert near and near[0].kind == "near"
    ext = find_duplicates([t("mine")], against=[t("public__repo-1")])
    assert ext[0].kind == "external" and ext[0].duplicate_of == "public__repo-1"


def test_scan_finds_secrets_and_personal_data():
    task = t("s", problem_statement="Fix login\n\nSigned-off-by: Jane Roe <jane@corp.io>",
             gold_patch=GOLD + "+AWS = 'AKIAABCDEFGHIJKLMNOP'\n+api_key = \"Zx9QpL2mN8vR4tY7wK1sD5fG3hJ6\"\n+demo = 'dev@example.com'\n")
    kinds = {(f.kind, f.severity) for f in scan_task(task)}
    assert ("aws_access_key", "secret") in kinds and ("high_entropy_secret", "secret") in kinds
    assert ("trailer", "pii") in kinds and ("email", "pii") in kinds
    assert not any("example.com" in f.excerpt for f in scan_task(task))
    clean = scan_task(t("c", gold_patch=GOLD + "+password = 'changeme-placeholder-value'\n"))
    assert clean == []


def test_redact_and_trailers():
    task = t("r", problem_statement="Fix it\n\nReported by ops@corp.io\n\nCo-authored-by: A <a@corp.io>")
    redact_task(task)
    assert task.problem_statement == "Fix it\n\nReported by <email>"
    assert strip_trailers("msg\n\nSigned-off-by: X <x@y.z>\n") == "msg"


def test_dedup_and_scan_cli(tmp_path):
    p = tmp_path / "t.jsonl"
    secret = t("leaky", gold_patch=GOLD + "+token = 'ghp_" + "a" * 36 + "'\n")
    save_tasks(p, [t("one"), t("twin"), secret])
    assert main(["dedup", str(p), "-o", str(tmp_path / "d.jsonl")]) == 0
    assert [x.instance_id for x in load_tasks(tmp_path / "d.jsonl")] == ["one", "leaky"]
    assert main(["scan", str(p), "-o", str(tmp_path / "s.jsonl"), "--redact"]) == 1
    assert [x.instance_id for x in load_tasks(tmp_path / "s.jsonl")] == ["one", "twin"]


def test_mined_problem_statements_have_no_trailers(demo_repo):
    import subprocess

    from swe_env_forge.mine import MineConfig, mine_commits

    path, _ = demo_repo
    repo = copy.deepcopy(path)
    tasks = mine_commits(str(repo), "d/m", MineConfig())
    assert all("Signed-off-by" not in x.problem_statement for x in tasks)
    assert subprocess.run(["git", "-C", str(repo), "log", "-1"], capture_output=True).returncode == 0
