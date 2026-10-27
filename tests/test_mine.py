from swe_env_kit.mine import MineConfig, keep, mine_commits, resolved_issues


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
