"""A small, original demo repository ("minishop") with a realistic commit history.

``sweforge demo DIR`` builds it so every stage can be tried end to end without Docker
or network access. The history contains:

1. the initial package and tests
2. a bug fix with a fail-to-pass test                          -> a good task
3. a feature whose tests import a brand-new function            -> a naming leak
4. a docs-only change                                           -> not a task (no tests)
5. a multi-file change with tests (>= 10 lines)                 -> a Pro-eligible task
6. a feature whose new test alternates pass / fail between runs -> a flaky task
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

FILES_V1 = {
    "README.md": "# minishop\n\nA tiny shop library used to demo swe-env-forge.\n",
    "pyproject.toml": '[project]\nname = "minishop"\nversion = "0.1.0"\n\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
    "minishop/__init__.py": "",
    "minishop/pricing.py": '''"""Prices are stored in cents."""


def apply_discount(cents, percent):
    """Return the price after a percentage discount."""
    return cents - cents * percent // 100


def format_price(cents):
    return f"${cents // 100}.{cents % 100:02d}"
''',
    "minishop/cart.py": '''from .pricing import apply_discount


class Cart:
    def __init__(self):
        self.items = []

    def add(self, name, cents, qty=1):
        self.items.append((name, cents, qty))

    def total(self, discount_percent=0):
        subtotal = sum(cents * qty for _name, cents, qty in self.items)
        return apply_discount(subtotal, discount_percent)
''',
    "minishop/inventory.py": '''class Inventory:
    def __init__(self, stock=None):
        self.stock = dict(stock or {})

    def take(self, name, qty=1):
        if self.stock.get(name, 0) < qty:
            raise ValueError(f"not enough {name}")
        self.stock[name] -= qty
''',
    "tests/__init__.py": "",
    "tests/test_cart.py": '''from minishop.cart import Cart


def test_total():
    c = Cart()
    c.add("pen", 150, qty=2)
    assert c.total() == 300


def test_empty_cart():
    assert Cart().total() == 0
''',
    "tests/test_pricing.py": '''from minishop.pricing import apply_discount, format_price


def test_format_price():
    assert format_price(1205) == "$12.05"


def test_no_discount():
    assert apply_discount(999, 0) == 999
''',
    "tests/test_inventory.py": '''import pytest

from minishop.inventory import Inventory


def test_take():
    inv = Inventory({"pen": 2})
    inv.take("pen")
    assert inv.stock["pen"] == 1


def test_take_too_many():
    with pytest.raises(ValueError):
        Inventory({"pen": 1}).take("pen", 2)
''',
}

# Commit 2: discounts round down, customers are overcharged by a cent.
FIX_ROUNDING = {
    "minishop/pricing.py": '''"""Prices are stored in cents."""


def apply_discount(cents, percent):
    """Return the price after a percentage discount, rounded half up to the nearest cent."""
    return cents - (cents * percent + 50) // 100


def format_price(cents):
    return f"${cents // 100}.{cents % 100:02d}"
''',
    "tests/test_pricing.py": FILES_V1["tests/test_pricing.py"] + '''

def test_discount_rounds_half_up():
    # 15% of 999 is 149.85 cents, so the discount is 150 cents
    assert apply_discount(999, 15) == 849
''',
}

# Commit 3: a feature whose tests import a function the issue never names.
FEATURE_REPORT = {
    "minishop/inventory.py": FILES_V1["minishop/inventory.py"] + '''

def low_stock_items(inventory, threshold=5):
    """Names of items at or below ``threshold``, sorted by remaining stock then name."""
    low = [(qty, name) for name, qty in inventory.stock.items() if qty <= threshold]
    return [name for _qty, name in sorted(low)]
''',
    "tests/test_inventory.py": FILES_V1["tests/test_inventory.py"] + '''

from minishop.inventory import low_stock_items


def test_low_stock_items():
    inv = Inventory({"pen": 1, "ink": 9, "pad": 1, "cap": 4})
    assert low_stock_items(inv) == ["pad", "pen", "cap"]
''',
}

DOCS = {"README.md": FILES_V1["README.md"] + "\nPrices are integers in cents.\n"}

# Commit 5: multi-file change, bulk discounts in cart and pricing.
BULK = {
    "minishop/pricing.py": FIX_ROUNDING["minishop/pricing.py"] + '''

def bulk_percent(qty, tiers=((10, 5), (50, 10), (100, 15))):
    """Discount percentage for buying ``qty`` units of one item."""
    percent = 0
    for min_qty, tier_percent in tiers:
        if qty >= min_qty:
            percent = tier_percent
    return percent
''',
    "minishop/cart.py": '''from .pricing import apply_discount, bulk_percent


class Cart:
    def __init__(self):
        self.items = []

    def add(self, name, cents, qty=1):
        self.items.append((name, cents, qty))

    def line_total(self, cents, qty):
        return apply_discount(cents * qty, bulk_percent(qty))

    def total(self, discount_percent=0):
        subtotal = sum(self.line_total(cents, qty) for _name, cents, qty in self.items)
        return apply_discount(subtotal, discount_percent)
''',
    "tests/test_cart.py": FILES_V1["tests/test_cart.py"] + '''

def test_bulk_discount_applies_per_line():
    c = Cart()
    c.add("pen", 100, qty=10)
    c.add("ink", 200, qty=1)
    assert c.total() == 950 + 200
''',
}

# Commit 6: a new test that passes and fails on alternate runs (state kept outside the repo).
FLAKY = {
    "minishop/orders.py": '''import itertools

_counter = itertools.count(1)


def next_order_id(prefix="ORD"):
    return f"{prefix}-{next(_counter):06d}"
''',
    "tests/test_orders.py": '''import os
import tempfile

from minishop.orders import next_order_id


def test_order_id_format():
    assert next_order_id().startswith("ORD-")


def test_order_ids_are_unique_under_load():
    # Deliberately flaky: shared state outside the repo flips the result every run.
    path = os.path.join(tempfile.gettempdir(), "minishop-flaky-counter")
    n = int(open(path).read()) if os.path.exists(path) else 0
    with open(path, "w") as fh:
        fh.write(str(n + 1))
    assert n % 2 == 0
''',
}

HISTORY = [
    ("Initial commit: cart, pricing and inventory", FILES_V1),
    ("Fix rounding when a discount is applied\n\nDiscounts were rounded down, so customers paid one cent too much\n"
     "for some prices. Round the discount half up instead.", FIX_ROUNDING),
    ("Add a low-stock report for the inventory\n\nShop owners want to see which items need restocking.", FEATURE_REPORT),
    ("Document that prices are in cents", DOCS),
    ("Support bulk discounts on large quantities\n\nBuying 10 or more of one item now gives a tiered discount\n"
     "on that line before any cart-wide discount.", BULK),
    ("Add order ids", FLAKY),
]


def _git(repo: Path, *args: str, env: dict | None = None) -> str:
    res = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr}")
    return res.stdout.strip()


def build_demo_repo(path: str | Path) -> list[str]:
    """Create the demo repository at ``path``; returns the commit shas, oldest first."""
    repo = Path(path)
    if repo.exists() and any(repo.iterdir()):
        raise FileExistsError(f"{repo} exists and is not empty")
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-q", "-b", "main")
    shas = []
    for i, (message, files) in enumerate(HISTORY):
        for rel, content in files.items():
            f = repo / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
        _git(repo, "add", "-A")
        date = f"2026-01-{i + 1:02d}T12:00:00+00:00"
        env = {**os.environ, "GIT_AUTHOR_NAME": "Demo Dev", "GIT_AUTHOR_EMAIL": "dev@example.com",
               "GIT_COMMITTER_NAME": "Demo Dev", "GIT_COMMITTER_EMAIL": "dev@example.com",
               "GIT_AUTHOR_DATE": date, "GIT_COMMITTER_DATE": date}
        _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message, env=env)
        shas.append(_git(repo, "rev-parse", "HEAD"))
    return shas
