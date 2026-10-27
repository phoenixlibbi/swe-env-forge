"""Environment presets for SWE-bench, SWE-bench Pro and mined tasks."""

from __future__ import annotations

import re
import shlex

from .task import EnvSpec

# --------------------------------------------------------------------- SWE-bench
SWEBENCH_WORKDIR = "/testbed"
SWEBENCH_SETUP = ["source /opt/miniconda3/bin/activate", "conda activate testbed",
                  "git config --global --add safe.directory /testbed"]

_PYTEST = "pytest --no-header -rA --tb=no -p no:cacheprovider {test_files}"
# repo -> (test command, log parser, test-file format). Used only when a dataset row
# carries no eval_script (older princeton-nlp copies); the SWE-bench/* copies include one.
SWEBENCH_REPOS: dict[str, tuple[str, str, str]] = {
    "django/django": ("./tests/runtests.py --verbosity 2 --settings=test_sqlite --parallel 1 {test_files}",
                      "django", "django"),
    "sympy/sympy": ("PYTHONWARNINGS='ignore::UserWarning,ignore::SyntaxWarning' bin/test -C --verbose {test_files}",
                    "sympy", "path"),
    "sphinx-doc/sphinx": ("tox --current-env -epy39 -v -- {test_files}", "pytest-v2", "path"),
    "astropy/astropy": ("pytest -rA -vv -o console_output_style=classic --tb=no {test_files}", "pytest-v2", "path"),
    "scikit-learn/scikit-learn": (_PYTEST, "pytest-v2", "path"),
    "matplotlib/matplotlib": (_PYTEST, "matplotlib", "path"),
    "mwaskom/seaborn": ("pytest --no-header -rA {test_files}", "seaborn", "path"),
    "psf/requests": ("pytest -rA {test_files}", "pytest-options", "path"),
    "pylint-dev/pylint": (_PYTEST, "pytest-options", "path"),
    "pydicom/pydicom": (_PYTEST, "pytest-options", "path"),
}

# SWE-bench's parser function names (the ``log_parser`` dataset column) -> ours.
SWEBENCH_PARSER_NAMES = {
    "parse_log_pytest": "pytest", "parse_log_pytest_options": "pytest-options",
    "parse_log_pytest_v2": "pytest-v2", "parse_log_django": "django", "parse_log_sympy": "sympy",
    "parse_log_seaborn": "seaborn", "parse_log_matplotlib": "matplotlib",
    "parse_log_astropy": "pytest-v2", "parse_log_scikit": "pytest-v2", "parse_log_sphinx": "pytest-v2",
    "parse_log_requests": "pytest-options", "parse_log_pylint": "pytest-options",
    "parse_log_pydicom": "pytest-options",
}


def swebench_image(instance_id: str, arch: str = "x86_64") -> str:
    """Official SWE-bench evaluation image for an instance (Docker Hub)."""
    return f"swebench/sweb.eval.{arch}.{instance_id.replace('__', '_1776_').lower()}:latest"


def swebench_parser(repo: str, dataset_name: str | None = None) -> str:
    if dataset_name:
        if dataset_name in SWEBENCH_PARSER_NAMES:
            return SWEBENCH_PARSER_NAMES[dataset_name]
        base = dataset_name.removeprefix("parse_log_")
        if base in ("django", "sympy", "seaborn", "matplotlib"):
            return base
    return SWEBENCH_REPOS.get(repo, (_PYTEST, "pytest", "path"))[1]


_START, _END = ">>>>> Start Test Output", ">>>>> End Test Output"


def parse_swebench_eval_script(script: str, base_commit: str) -> tuple[list[str], str] | None:
    """Pull (pre-test commands, test command) out of a SWE-bench ``eval_script``.

    The script resets test files, applies the test patch and runs the tests between
    two marker lines. We keep the install steps that come before the reset, and the
    test command between the markers. The harness itself applies patches.
    """
    lines = script.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if _START in ln)
        end = next(i for i, ln in enumerate(lines) if _END in ln and i > start)
    except StopIteration:
        return None
    test_cmd = " && ".join(ln.strip() for ln in lines[start + 1:end] if ln.strip())
    pre: list[str] = []
    after_diff = False
    skip = ("source /opt/miniconda3/bin/activate", "conda activate testbed", "cd /testbed",
            "git status", "git show", "git config --global --add safe.directory")
    for ln in lines[:start]:
        s = ln.strip()
        if s.startswith("git -c core.fileMode=false diff"):
            after_diff = True
            continue
        if not after_diff or not s or s.startswith("#"):
            continue
        if s.startswith(f"git checkout {base_commit}") or s.startswith("git apply"):
            break
        if any(s.startswith(k) for k in skip):
            continue
        pre.append(s)
    return pre, test_cmd


def swebench_env(repo: str, instance_id: str, row: dict | None = None) -> EnvSpec:
    row = row or {}
    image = row.get("image") or swebench_image(instance_id)
    parser = swebench_parser(repo, row.get("log_parser"))
    parsed = parse_swebench_eval_script(row["eval_script"], row.get("base_commit", "")) if row.get("eval_script") else None
    if parsed:
        pre, cmd = parsed
        return EnvSpec(image=image, platform="linux/amd64", workdir=SWEBENCH_WORKDIR, setup=list(SWEBENCH_SETUP),
                       pre_test=pre, test_command=cmd, test_file_format="none", log_parser=parser,
                       start_ref="image-head")
    cmd, _p, fmt = SWEBENCH_REPOS.get(repo, (_PYTEST, "pytest", "path"))
    return EnvSpec(image=image, platform="linux/amd64", workdir=SWEBENCH_WORKDIR, setup=list(SWEBENCH_SETUP),
                   test_command=cmd, test_file_format=fmt, log_parser=parser, start_ref="image-head")


# ------------------------------------------------------------------ SWE-bench Pro
PRO_WORKDIR = "/app"
PRO_SCRIPTS_REPO = "scaleapi/SWE-bench_Pro-os"
PRO_LANGUAGES = {"py": "python", "python": "python", "js": "javascript", "javascript": "javascript",
                 "ts": "typescript", "typescript": "typescript", "go": "go"}


def pro_env(row: dict, run_script: str | None = None, parser_script: str | None = None) -> EnvSpec:
    """Pro tasks run a per-instance ``run_script.sh`` and ``parser.py`` (fetched by ``hub``)."""
    image = row.get("docker_image") or (f"jefzda/sweap-images:{row['dockerhub_tag']}" if row.get("dockerhub_tag") else None)
    files = row.get("selected_test_files_to_run") or []
    if isinstance(files, str):
        from .task import _as_list

        files = _as_list(files)
    return EnvSpec(image=image, platform="linux/amd64", workdir=PRO_WORKDIR, test_files=list(files),
                   test_file_format="comma", log_parser="json", run_script=run_script,
                   parser_script=parser_script, hidden_paths=["/tests/test_patch.patch"])


# --------------------------------------------------------------- test-file rendering
def render_test_files(files: list[str], fmt: str) -> str:
    """Render test files for the ``{test_files}`` placeholder."""
    if fmt == "none":
        return ""
    if fmt == "comma":
        return shlex.quote(",".join(files)) if files else "''"
    if fmt in ("dotted-module", "django"):
        mods = []
        for f in files:
            f = re.sub(r"\.py$", "", f)
            if fmt == "django":
                f = re.sub(r"^tests/", "", f)
            mods.append(f.replace("/", "."))
        return " ".join(shlex.quote(m) for m in mods)
    if fmt == "go-package":
        pkgs = sorted({"./" + f.rsplit("/", 1)[0] if "/" in f else "." for f in files})
        return " ".join(shlex.quote(p) for p in pkgs)
    return " ".join(shlex.quote(f) for f in files)


# ------------------------------------------------------------ language templates
# Default environments for mined tasks. Each provides a base image, install steps,
# a test command and a parser. They are starting points: real repos often need tweaks.
LANGUAGE_TEMPLATES: dict[str, dict] = {
    "python": {"base": "python:3.11-slim", "packages": "git build-essential",
               "install": ["pip install --no-cache-dir -U pip pytest",
                           "if [ -f requirements-dev.txt ]; then pip install --no-cache-dir -r requirements-dev.txt || true; fi",
                           "if [ -f requirements.txt ]; then pip install --no-cache-dir -r requirements.txt || true; fi",
                           "pip install --no-cache-dir -e '.[test]' || pip install --no-cache-dir -e '.[dev]' || pip install --no-cache-dir -e . || true"],
               "test_command": "python -m pytest -rA --tb=short -p no:cacheprovider {test_files}",
               "log_parser": "pytest", "test_file_format": "path"},
    "go": {"base": "golang:1.22", "packages": "git", "install": ["go mod download || true", "go build ./... || true"],
           "test_command": "go test -json {test_files}", "log_parser": "go", "test_file_format": "go-package"},
    "javascript": {"base": "node:20-bookworm", "packages": "git",
                   "install": ["if [ -f package-lock.json ]; then npm ci; elif [ -f yarn.lock ]; then corepack enable && yarn install --frozen-lockfile; elif [ -f pnpm-lock.yaml ]; then corepack enable && pnpm install --frozen-lockfile; else npm install; fi"],
                   "test_command": "npx jest --ci --json {test_files} 2>/dev/null", "log_parser": "jest", "test_file_format": "path"},
    "rust": {"base": "rust:1-bookworm", "packages": "git", "install": ["cargo fetch", "cargo build --tests || true"],
             "test_command": "cargo test --no-fail-fast 2>&1", "log_parser": "cargo", "test_file_format": "none"},
    "java": {"base": "maven:3-eclipse-temurin-21", "packages": "git",
             "install": ["mvn -q -B dependency:go-offline || true", "mvn -q -B -DskipTests install || true"],
             "test_command": "mvn -B -q test -Dsurefire.useFile=true -DfailIfNoTests=false; cat target/surefire-reports/*.xml 2>/dev/null",
             "log_parser": "junit", "test_file_format": "none"},
    "csharp": {"base": "mcr.microsoft.com/dotnet/sdk:8.0", "packages": "git", "install": ["dotnet restore || true", "dotnet build || true"],
               "test_command": "dotnet test --logger 'console;verbosity=normal' 2>&1", "log_parser": "dotnet", "test_file_format": "none"},
    "ruby": {"base": "ruby:3.3", "packages": "git", "install": ["bundle install || true"],
             "test_command": "bundle exec rspec --format json {test_files}", "log_parser": "rspec", "test_file_format": "path"},
    "php": {"base": "php:8.3-cli", "packages": "git unzip",
            "install": ["curl -sS https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer",
                        "composer install --no-interaction || true"],
            "test_command": "vendor/bin/phpunit --log-junit /tmp/junit.xml {test_files}; cat /tmp/junit.xml",
            "log_parser": "junit", "test_file_format": "path"},
}
LANGUAGE_TEMPLATES["typescript"] = dict(LANGUAGE_TEMPLATES["javascript"])
LANGUAGE_TEMPLATES["kotlin"] = dict(LANGUAGE_TEMPLATES["java"])
