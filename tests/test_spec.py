import copy
import json

from swe_env_forge.llm import ChatClient, LLMError
from swe_env_forge.spec import augment, draft_interface, draft_requirements, leak_check
from swe_env_forge.task import Task
from swe_env_forge.validate import validate


def test_interface_for_new_function(demo_tasks):
    items = draft_interface(demo_tasks["feature"])
    assert len(items) == 1
    it = items[0]
    assert (it.kind, it.name, it.location) == ("Function", "low_stock_items", "minishop/inventory.py")
    assert it.signature == "def low_stock_items(inventory, threshold=5)"
    assert it.description.startswith("Names of items at or below")
    assert it.change == "new"


def test_interface_methods_and_private_names():
    gold = ("diff --git a/shop/cart.py b/shop/cart.py\n--- a/shop/cart.py\n+++ b/shop/cart.py\n@@ -1,2 +1,7 @@\n"
            " class Cart:\n     items = []\n+\n+    def line_total(self, cents, qty):\n+        return cents * qty\n"
            "+\n+    def _helper(self):\n+        return 1\n")
    tests = ("diff --git a/tests/test_c.py b/tests/test_c.py\n--- /dev/null\n+++ b/tests/test_c.py\n@@ -0,0 +1,2 @@\n"
             "+def test_line():\n+    assert Cart().line_total(2, 3) == 6 and Cart()._helper()\n")
    t = Task(instance_id="i", repo="r", base_commit="b", gold_patch=gold, test_patch=tests)
    items = draft_interface(t)
    # _helper is "private" but the tests call it, so the agent must be told about it
    assert [(i.kind, i.name) for i in items] == [("Method", "Cart.line_total"), ("Method", "Cart._helper")]


def test_requirements_draft_collects_assertions_and_exact_strings():
    gold = ('diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,2 @@\n x = 1\n'
            '+    raise ValueError("quantity must be a positive integer")\n')
    tests = ('diff --git a/tests/test_a.py b/tests/test_a.py\n--- /dev/null\n+++ b/tests/test_a.py\n@@ -0,0 +1,4 @@\n'
             '+def test_bad_qty():\n+    with pytest.raises(ValueError, match="quantity must be a positive integer"):\n'
             '+        take(-1)\n+    assert True\n')
    t = Task(instance_id="i", repo="r", base_commit="b", gold_patch=gold, test_patch=tests)
    d = draft_requirements(t)
    assert d.exact_strings == ["quantity must be a positive integer"]
    assert "test_bad_qty" in d.checks
    text = d.render()
    assert text.startswith("DRAFT") and 'exactly the text "quantity must be a positive integer"' in text


def test_leak_check(demo_tasks):
    t = demo_tasks["bulk"]
    clean = leak_check("Buying 10 or more of one item should give a tiered discount on that line.", t.gold_patch)
    assert not clean.leaked and clean.score == 0.0
    leaky = leak_check("Change cart to: return apply_discount(cents * qty, bulk_percent(qty)) everywhere", t.gold_patch)
    assert leaky.leaked and leaky.copied_lines == ["return apply_discount(cents * qty, bulk_percent(qty))"]


def test_leak_check_allows_interface_signatures(demo_tasks):
    t = demo_tasks["feature"]
    iface = "Function: low_stock_items\nSignature: def low_stock_items(inventory, threshold=5)"
    assert not leak_check(iface, t.gold_patch, allow=iface).leaked


def test_augment_fixes_the_naming_leak(demo_tasks):
    t = copy.deepcopy(demo_tasks["feature"])
    t, report = augment(t)
    assert "Function: low_stock_items" in t.interface
    assert t.requirements.startswith("DRAFT")
    assert report["leak"]["problem_statement"]["leaked"] is False
    res = validate(t, backend="local", runs=1)
    assert res.verdict == "valid" and "naming_leak" not in res.warnings
    assert any(e["symbol"] == "low_stock_items" and e["specified"] for e in res.naming_errors)


class FakeClient:
    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return self.reply, None


def test_augment_with_llm_rewrites_problem_statement(demo_tasks):
    t = copy.deepcopy(demo_tasks["bulk"])
    client = FakeClient("Large orders get no discount\n\nBuying many units of one item should be cheaper per unit.")
    t, report = augment(t, client)
    assert t.problem_statement.startswith("Large orders get no discount")
    assert "do not leak" in client.calls[0][1]["content"]
    assert t.metadata["spec"]["leak"] is False


def test_chat_client_requires_model():
    try:
        ChatClient(model="").chat([{"role": "user", "content": "hi"}])
    except LLMError as exc:
        assert "no model" in str(exc)
    else:
        raise AssertionError("expected LLMError")


def test_chat_client_against_local_stub():
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert body["model"] == "m" and self.headers["Authorization"] == "Bearer k"
            out = json.dumps({"choices": [{"message": {"content": "hello"}}],
                              "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        c = ChatClient(model="m", base_url=f"http://127.0.0.1:{srv.server_port}/v1", api_key="k",
                       input_price=3.0, output_price=15.0)
        text, usage = c.chat([{"role": "user", "content": "x"}])
        assert text == "hello" and usage.prompt_tokens == 1000
        assert abs(usage.cost - (1000 * 3 + 500 * 15) / 1e6) < 1e-12
        assert c.usage.calls == 1
    finally:
        srv.shutdown()


# ------------------------------------------------------------ authoring modes
import json as _json

from swe_env_forge.spec import LLMWriter, export_review_file, import_review_file


class JSONClient:
    model = "fake-model"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def chat(self, messages, **kw):
        self.calls.append(messages)
        return self.replies.pop(0), None


def test_llm_writer_fills_all_three_fields(demo_tasks):
    t = copy.deepcopy(demo_tasks["feature"])
    reply = _json.dumps({"problem_statement": "Owners cannot see which items are running low\n\nShow them.",
                         "requirements": ["Items at or below the threshold are reported", "Sorted by stock, then name"],
                         "interface_descriptions": {"low_stock_items": "Returns names of low-stock items."}})
    client = JSONClient(["```json\n" + reply + "\n```"])
    t, report = augment(t, writer=LLMWriter(client))
    assert t.problem_statement.startswith("Owners cannot see")
    assert t.requirements == "- Items at or below the threshold are reported\n- Sorted by stock, then name"
    # the code's own docstring wins over the model's description
    assert "Description: Names of items at or below" in t.interface
    assert t.metadata["spec"]["mode"] == "llm" and t.metadata["spec"]["fields_by"]["requirements"] == "llm:fake-model"
    assert report["writer"]["attempts"] == 1


def test_llm_writer_retries_when_it_leaks_the_fix(demo_tasks):
    t = copy.deepcopy(demo_tasks["bulk"])
    leaky = _json.dumps({"problem_statement": "Do this: return apply_discount(cents * qty, bulk_percent(qty))",
                         "requirements": []})
    clean = _json.dumps({"problem_statement": "Large orders should get a tiered discount per line.",
                         "requirements": ["10+ units of one item get 5% off that line"]})
    client = JSONClient([leaky, clean])
    t, report = augment(t, writer=LLMWriter(client))
    assert report["writer"]["attempts"] == 2 and not t.metadata["spec"]["leak"]
    assert "copies code from the reference fix" in client.calls[1][-1]["content"]


class MyWriter:
    name = "team-agent"

    def write(self, task, drafts):
        return {"requirements": "- custom requirement", "problem_statement": "Custom statement"}


def test_custom_writer(demo_tasks):
    t, _ = augment(copy.deepcopy(demo_tasks["fix"]), writer=MyWriter())
    assert t.requirements == "- custom requirement"
    assert t.metadata["spec"]["mode"] == "custom" and t.metadata["spec"]["fields_by"]["problem_statement"] == "team-agent"


def test_review_file_roundtrip(demo_tasks):
    t, _ = augment(copy.deepcopy(demo_tasks["feature"]))
    text = export_review_file(t)
    assert "status: draft" in text and "### Reference fix" in text
    edited = (text.replace("status: draft", "status: reviewed")
                  .replace("reviewer:        # your name", "reviewer: Ayesha")
                  .replace(t.requirements, "- Items at or below the threshold are listed, lowest stock first."))
    r = import_review_file(t, edited)
    assert r["reviewed"] and r["reviewer"] == "Ayesha" and r["changed"] == ["requirements"]
    assert t.requirements == "- Items at or below the threshold are listed, lowest stock first."
    assert t.metadata["spec"]["fields_by"]["requirements"] == "human:Ayesha"
    import pytest as _pytest

    other = copy.deepcopy(demo_tasks["fix"])
    with _pytest.raises(ValueError):
        import_review_file(other, edited)
