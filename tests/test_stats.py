from swe_env_kit.stats import license_class, summarize, task_stats
from swe_env_kit.task import Task

GOLD = """\
diff --git a/shop/cart.py b/shop/cart.py
--- a/shop/cart.py
+++ b/shop/cart.py
@@ -1,2 +1,4 @@
 class Cart:
-    pass
+    def apply_discount(self, pct):
+        return pct
"""
TESTS = """\
diff --git a/tests/test_cart.py b/tests/test_cart.py
--- /dev/null
+++ b/tests/test_cart.py
@@ -0,0 +1,2 @@
+def test_discount():
+    assert Cart().apply_discount(10) == 10
"""


def make(**kw):
    d = dict(instance_id="i", repo="o/r", base_commit="b", gold_patch=GOLD, test_patch=TESTS,
             problem_statement="Carts need discounts.", fail_to_pass=["t1"])
    d.update(kw)
    return Task.from_dict(d)


def test_naming_leak_detected_and_cleared_by_interface():
    s = task_stats(make())
    assert s.new_symbols_used_by_tests == ["apply_discount"]
    assert s.naming_leak
    s2 = task_stats(make(interface="Function: apply_discount\nLocation: shop/cart.py"))
    assert not s2.naming_leak


def test_size_flags():
    s = task_stats(make())
    assert s.gold["lines_edited"] == 2 and s.trivial and not s.pro_eligible
    assert s.language == "python"
    assert task_stats(make(), min_lines=3).pro_eligible


def test_license_class():
    assert license_class("GPL-3.0") == "copyleft"
    assert license_class("LGPL-2.1-only") == "copyleft"
    assert license_class("AGPL-3.0") == "copyleft"
    assert license_class("MIT") == "permissive"
    assert license_class("BSD-3-Clause") == "permissive"
    assert license_class("NOASSERTION") == "unknown"
    assert license_class(None) == "unknown"


def test_summarize():
    s = summarize([task_stats(make()), task_stats(make(repo="o/q", instance_id="j"))])
    assert s["tasks"] == 2 and s["repos"] == 2
    assert s["trivial_1_2_lines"] == {"count": 2, "pct": 100.0}
    assert s["naming_leak_unspecified"]["count"] == 2
    assert summarize([])["tasks"] == 0
