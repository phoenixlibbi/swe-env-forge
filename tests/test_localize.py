from swe_env_forge.localize import (BM25, build_prompt, extract_patch, fill_budget, localize_task, oracle_files,
                                  rank_files, read_files, summarize, tokenize)
from swe_env_forge.workspace import repo_cache


def test_tokenize_splits_identifiers():
    toks = tokenize("def getOrCreate(low_stock_items): HTTPServer")
    assert {"getorcreate", "get", "or", "create", "low", "stock", "items", "http", "server"} <= set(toks)


def test_bm25_prefers_rare_matching_terms():
    docs = [tokenize("the cart total"), tokenize("the bulk discount tiers"), tokenize("the the the")]
    s = BM25(docs).scores(tokenize("bulk discount"))
    assert s[1] > s[0] == s[2] == 0.0


def test_read_files_from_git_objects(demo_tasks):
    t = demo_tasks["bulk"]
    files = read_files(repo_cache(t.clone_url, t.base_commit), t.base_commit)
    assert "minishop/pricing.py" in files and "tests/test_cart.py" not in files
    assert "README.md" not in files  # code files only by default
    with_tests = read_files(repo_cache(t.clone_url), t.base_commit, include_tests=True)
    assert "tests/test_cart.py" in with_tests


def test_localize_task_and_summary(demo_tasks):
    t = demo_tasks["bulk"]
    assert sorted(oracle_files(t)) == ["minishop/cart.py", "minishop/pricing.py"]
    loc = localize_task(t, budgets=(60, 100_000))
    assert loc.n_files == 4
    assert loc.by_budget["100000"]["all"] and loc.by_budget["100000"]["avg"] == 1.0
    assert loc.by_budget["60"]["files"] <= 1
    assert loc.first_hit_rank is not None
    s = summarize([loc])
    assert s["tasks"] == 1 and s["budgets"]["100000"]["all"] == 100.0 and 0 < s["mrr"] <= 1


def test_new_files_are_not_oracle_targets(demo_tasks):
    assert oracle_files(demo_tasks["flaky"]) == []  # the fix only adds a new module
    assert summarize([localize_task(demo_tasks["flaky"], budgets=(1000,))]) == {"tasks": 0}


def test_fill_budget_stops_at_first_overflow():
    files = {"a": "x" * 400, "b": "y" * 4000, "c": "z" * 40}
    assert fill_budget(["a", "b", "c"], files, budget=200) == ["a"]
    assert rank_files(files, "zzz")[0] in files


def test_prompt_and_patch_extraction(demo_tasks):
    t = demo_tasks["fix"]
    files = {"minishop/pricing.py": "def f():\n    return 1\n"}
    prompt = build_prompt(t, files, list(files))
    assert "<issue>" in prompt and "[start of minishop/pricing.py]\n1 def f():" in prompt
    reply = "Sure!\n<patch>\ndiff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n</patch>"
    assert extract_patch(reply).startswith("diff --git a/x.py")
    fenced = "```diff\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n```"
    assert extract_patch(fenced).startswith("--- a/x.py")
    assert extract_patch("no patch here") == ""
