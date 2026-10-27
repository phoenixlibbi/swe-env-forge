from swe_env_forge.mine import MineConfig, keep, mine_commits, resolved_issues


def test_mine_commits_finds_the_four_tasks(demo_repo, demo_tasks):
    _path, shas = demo_repo
    commits = [t.metadata["instance_commit"] for t in demo_tasks["all"]]
    assert commits == [shas[5], shas[4], shas[2], shas[1]]  # newest first, docs-only and root skipped


def test_task_shape(demo_tasks):
    t = demo_tasks["fix"]
    assert t.base_commit == demo_tasks["shas"][0]
    assert t.language == "python" and t.source == "mined-commits"
    assert t.problem_statement.startswith("Fix rounding")
    assert "minishop/pricing.py" in t.gold_patch and "tests/" not in t.gold_patch
    assert "tests/test_pricing.py" in t.test_patch
    assert t.env.test_command and t.env.log_parser == "pytest"
    assert t.test_files() == ["tests/test_pricing.py"]


def test_pro_filters_keep_only_the_big_change(demo_repo, demo_tasks):
    path, shas = demo_repo
    skipped = []
    tasks = mine_commits(str(path), "demo/minishop", MineConfig.pro(), on_skip=lambda s, why: skipped.append(why))
    assert [t.metadata["instance_commit"] for t in tasks] == [shas[4]]
    assert "too_small" in skipped and "no_test_changes" in skipped


def test_max_tasks(demo_repo):
    path, _ = demo_repo
    assert len(mine_commits(str(path), "d/m", MineConfig(max_tasks=2))) == 2


def test_keep_reasons():
    assert keep("", "", MineConfig()) == (False, "no_test_changes")
    assert keep("", "diff --git a/tests/t.py b/tests/t.py\n", MineConfig()) == (False, "no_code_changes")


def test_resolved_issues():
    text = "Fixes #12, closes owner/repo#7 and resolved: #3. Mentions #99 only."
    assert resolved_issues(text) == [3, 7, 12]
    assert resolved_issues("prefix#4 fixes") == []


def _commit(repo, files, msg, author="dev", date="2026-02-01T10:00:00+00:00"):
    import os
    import subprocess

    for path, text in files.items():
        p = repo / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    env = {**os.environ, "GIT_AUTHOR_NAME": author, "GIT_AUTHOR_EMAIL": f"{author}@x", "GIT_COMMITTER_NAME": author,
           "GIT_COMMITTER_EMAIL": f"{author}@x", "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", msg], check=True, env=env)


def test_window_merges_code_and_later_tests(tmp_path):
    import subprocess

    repo = tmp_path / "app"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    _commit(repo, {"app/core.py": "def f():\n    return 1\n", "tests/test_core.py": "def test_f():\n    pass\n"}, "init")
    _commit(repo, {"app/core.py": "def f():\n    return 2\n"}, "change f", date="2026-02-02T09:00:00+00:00")
    _commit(repo, {"tests/test_core.py": "from app.core import f\n\ndef test_f():\n    assert f() == 2\n"},
            "test f", date="2026-02-02T17:00:00+00:00")
    _commit(repo, {"README.md": "x\n"}, "docs", date="2026-02-03T09:00:00+00:00")
    assert mine_commits(str(repo), "o/app", MineConfig()) == []          # no single commit has code + tests
    tasks = mine_commits(str(repo), "o/app", MineConfig(window=2))
    assert len(tasks) == 1
    t = tasks[0]
    assert [m.splitlines()[0] for m in t.problem_statement.split("\n\n")] == ["change f", "test f"]
    assert "app/core.py" in t.gold_patch and "tests/test_core.py" in t.test_patch
    assert len(t.metadata["commits"]) == 2
    # different authors on the same day cannot be merged with group_by=author
    other = tmp_path / "other"
    other.mkdir()
    subprocess.run(["git", "init", "-q", str(other)], check=True)
    _commit(other, {"app/core.py": "def f():\n    return 1\n", "tests/test_core.py": "def test_f():\n    pass\n"}, "init")
    _commit(other, {"app/core.py": "def f():\n    return 2\n"}, "change f", author="alice")
    _commit(other, {"tests/test_core.py": "def test_f():\n    assert 1\n"}, "test f", author="bob")
    assert mine_commits(str(other), "o/x", MineConfig(window=2, group_by="author")) == []
    assert len(mine_commits(str(other), "o/x", MineConfig(window=2))) == 1
