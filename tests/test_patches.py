from swe_env_forge.patches import (added_definitions, added_lines, files_in, identifiers, parse_patch,
                                 patch_stats, split_patch, strip_binary)

FIX = """\
diff --git a/shop/cart.py b/shop/cart.py
index 1111111..2222222 100644
--- a/shop/cart.py
+++ b/shop/cart.py
@@ -1,5 +1,9 @@
 class Cart:
-    def total(self):
-        return sum(self.items)
+    def total(self):
+        return sum(i.price for i in self.items)
+
+    def apply_discount(self, pct):
+        return self.total() * (1 - pct / 100)
-- a line that starts with two dashes
 
diff --git a/tests/test_cart.py b/tests/test_cart.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_cart.py
@@ -0,0 +1,4 @@
+from shop.cart import Cart
+
+def test_discount():
+    assert Cart().apply_discount(10) == 0
diff --git a/old.txt b/new.txt
similarity index 100%
rename from old.txt
rename to new.txt
diff --git a/logo.png b/logo.png
index 1..2 100644
Binary files a/logo.png and b/logo.png differ
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-def gone():
-    pass
"""


def test_parse_files_and_flags():
    files = parse_patch(FIX)
    assert [f.path for f in files] == ["shop/cart.py", "tests/test_cart.py", "new.txt", "logo.png", "gone.py"]
    cart, test, ren, png, gone = files
    assert test.added_file and not cart.added_file
    assert ren.renamed and ren.old_path == "old.txt"
    assert png.binary
    assert gone.deleted and gone.path == "gone.py"
    assert test.is_test and not cart.is_test


def test_dash_dash_inside_hunk_is_a_removed_line():
    cart = parse_patch(FIX)[0]
    assert "- a line that starts with two dashes" in cart.removed


def test_split_patch():
    src, tst = split_patch(FIX)
    assert files_in(tst) == ["tests/test_cart.py"]
    assert "tests/test_cart.py" not in files_in(src)
    assert "shop/cart.py" in files_in(src)
    # the split is lossless
    assert sorted(files_in(src) + files_in(tst)) == sorted(files_in(FIX))


def test_stats_ignore_blank_lines():
    s = patch_stats(split_patch(FIX)[0])
    assert s.files == 4 and s.code_files == 2
    assert s.lines_added == 4  # blank added line not counted
    assert s.lines_removed == 5
    assert s.lines_changed == 9 and s.lines_edited == 5


def test_added_definitions_marks_new_vs_changed():
    defs = {d.name: d for d in added_definitions(FIX)}
    assert defs["apply_discount"].is_new
    assert not defs["total"].is_new
    assert defs["test_discount"].path == "tests/test_cart.py"
    assert defs["apply_discount"].signature == "def apply_discount(self, pct):"


def test_added_lines_filters_tests():
    assert "from shop.cart import Cart" in added_lines(FIX, only_tests=True)
    assert all("assert" not in ln for ln in added_lines(FIX, only_tests=False))
    assert "apply_discount" in identifiers(added_lines(FIX, only_tests=True))


def test_strip_binary():
    assert "logo.png" not in files_in(strip_binary(FIX))


def test_plain_unified_diff_without_git_headers():
    plain = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a = 1\n+a = 2\n--- a/y.py\n+++ b/y.py\n@@ -1 +1 @@\n-b\n+c\n"
    files = parse_patch(plain)
    assert [f.path for f in files] == ["x.py", "y.py"]
    assert files[0].added == ["a = 2"]


def test_empty():
    assert parse_patch("") == [] and parse_patch(None) == []
    assert patch_stats("").lines_changed == 0
