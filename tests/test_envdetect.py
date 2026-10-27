import json
import subprocess

import pytest

from swe_env_kit.envdetect import (RepoView, apply_plan, detect, dockerfile_for, pick_python,
                                   _python_from_requires)
from swe_env_kit.task import Task
from swe_env_kit.yamlite import _Parser, loads

WORKFLOW = """
name: Tests
on:
  push:
    branches: [main]
jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - run: ruff check .
  tests:
    name: py${{ matrix.python }}
    runs-on: ubuntu-latest
    strategy:
      matrix:
        python: ['3.10', '3.11', '3.12']
        include:
          - {python: 'pypy-3.10'}
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_PASSWORD: secret   # comment
        ports: ['5432:5432']
      cache:
        image: redis:7
    env:
      DATABASE_URL: postgres://postgres:secret@localhost:5432/postgres
      TOKEN: ${{ secrets.TOKEN }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: ${{ matrix.python }}
      - name: Test
        run: |
          pip install -e .[test]
          pytest -q
"""


def test_yamlite_parses_ci_workflows():
    d = _Parser(WORKFLOW).parse()
    job = d["jobs"]["tests"]
    assert job["strategy"]["matrix"]["python"] == ["3.10", "3.11", "3.12"]
    assert job["strategy"]["matrix"]["include"] == [{"python": "pypy-3.10"}]
    assert job["services"]["postgres"]["env"] == {"POSTGRES_PASSWORD": "secret"}
    assert job["steps"][1]["with"]["python-version"] == "${{ matrix.python }}"
    assert job["steps"][2]["run"] == "pip install -e .[test]\npytest -q\n"
    assert d["jobs"]["lint"]["steps"] == [{"run": "ruff check ."}]


def test_yamlite_flow_and_scalars():
    d = _Parser("a: [1, 'two', {x: y}]\nb: true\nc: ~\nd: >\n  folded\n  text\ne: \"q: x\"\n").parse()
    assert d == {"a": [1, "two", {"x": "y"}], "b": True, "c": None, "d": "folded text\n", "e": "q: x"}
    assert loads("k: v") == {"k": "v"}


def test_python_version_helpers():
    assert pick_python(["3.8", "3.13", "3.14", "3.12.1"]) == "3.13"
    assert pick_python([]) == "3.11"
    assert _python_from_requires(">=3.9,<3.12") == ["3.9", "3.10", "3.11"]


def _repo(tmp_path, files):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for path, text in files.items():
        p = tmp_path / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@x", "commit", "-qm", "c"],
                   check=True)
    sha = subprocess.run(["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    return RepoView(tmp_path, sha)


def test_detect_python_repo_with_services(tmp_path):
    view = _repo(tmp_path, {
        ".github/workflows/ci.yml": WORKFLOW,
        "pyproject.toml": '[project]\nname="x"\nrequires-python=">=3.9"\n[project.optional-dependencies]\ntest=["pytest"]\n',
        "requirements/tests.txt": "freezegun\n",
        "tox.ini": "[testenv]\ndeps =\n    -r requirements/tests.txt\n    coverage\ncommands = pytest\n",
        "src/x/__init__.py": "",
    })
    plan = detect(view)
    assert plan.language == "python" and plan.runtime_version == "3.12" and plan.base_image == "python:3.12-bookworm"
    assert any("requirements/tests.txt" in c for c in plan.install)
    assert any("coverage" in c for c in plan.install)
    assert any(".[test]" in c for c in plan.install)
    assert [s["image"] for s in plan.services] == ["postgres:16", "redis:7"]
    assert plan.services[0]["ports"] == [5432] and plan.services[1]["ports"] == [6379]
    assert plan.env_vars == {"DATABASE_URL": "postgres://postgres:secret@localhost:5432/postgres"}  # secrets skipped
    assert any("from .github/workflows/ci.yml" in e for e in plan.evidence)


def test_detect_node_go_rust_java(tmp_path):
    node = detect(_repo(tmp_path / "n", {"package.json": json.dumps({"devDependencies": {"vitest": "1"},
                                                                     "engines": {"node": ">=18"}}),
                                         "pnpm-lock.yaml": "", ".nvmrc": "v22.3.0\n", "tsconfig.json": "{}"}))
    assert (node.language, node.runtime_version, node.log_parser) == ("typescript", "22", "jest")
    assert node.install == ["corepack enable && pnpm install --frozen-lockfile"]
    assert "vitest run --reporter=json" in node.test_command
    go = detect(_repo(tmp_path / "g", {"go.mod": "module x\n\ngo 1.21\n"}))
    assert go.base_image == "golang:1.21-bookworm" and go.test_file_format == "go-package"
    rust = detect(_repo(tmp_path / "r", {"Cargo.toml": "[package]\n", "rust-toolchain.toml": '[toolchain]\nchannel = "1.79.0"\n'}))
    assert rust.base_image == "rust:1.79.0-bookworm"
    java = detect(_repo(tmp_path / "j", {"pom.xml": "<project><properties><maven.compiler.release>17</maven.compiler.release></properties></project>"}))
    assert java.base_image == "maven:3-eclipse-temurin-17" and java.log_parser == "junit"


def test_compose_services_only_infrastructure(tmp_path):
    view = _repo(tmp_path, {"go.mod": "module x\ngo 1.22\n",
                            "docker-compose.yml": "services:\n  app:\n    build: .\n  db:\n    image: mysql:8\n"
                                                  "    environment:\n      - MYSQL_ROOT_PASSWORD=pw\n  web:\n    image: nginx\n"})
    plan = detect(view)
    assert [(s["name"], s["image"], s["ports"]) for s in plan.services] == [("db", "mysql:8", [3306])]
    assert plan.services[0]["env"] == {"MYSQL_ROOT_PASSWORD": "pw"}


def test_apply_plan_writes_dockerfile_and_env(tmp_path):
    view = _repo(tmp_path, {"pyproject.toml": '[project]\nname="x"\n', ".python-version": "3.10\n"})
    plan = detect(view)
    t = Task(instance_id="i", repo="o/r", base_commit=view.commit, repo_url=str(tmp_path), language="python")
    apply_plan(t, plan)
    assert t.env.dockerfile.startswith("FROM python:3.10-bookworm")
    assert "COPY repo.bundle" in t.env.dockerfile and t.env.log_parser == "pytest"
    assert t.metadata["env_plan"]["runtime_version"] == "3.10"
    assert "git clone -q https://github.com/o/r.git" in dockerfile_for(plan, Task("i", "o/r", "b"), local_bundle=False)
