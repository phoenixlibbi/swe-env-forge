"""Detect how a repository builds and tests, from its own files, at the task's base commit.

The most reliable description of a repository's environment is the one its maintainers
already wrote: CI workflows, version files, dependency manifests, tox / compose files. This
module reads them straight from git objects (nothing is executed) and produces an
:class:`EnvPlan`: runtime and version, system packages, install steps, test command and log
parser, and backing services. Every decision records the file it came from (``evidence``),
so a human, or the setup agent, can see why the environment looks the way it does.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .task import EnvSpec, Task
from .yamlite import YAMLError, loads

PY_SUPPORTED = ("3.8", "3.9", "3.10", "3.11", "3.12", "3.13")
KNOWN_SERVICES = {   # image name fragment -> default port
    "postgres": 5432, "postgis": 5432, "mysql": 3306, "mariadb": 3306, "redis": 6379, "valkey": 6379,
    "mongo": 27017, "rabbitmq": 5672, "elasticsearch": 9200, "opensearch": 9200, "memcached": 11211,
    "minio": 9000, "localstack": 4566, "zookeeper": 2181, "kafka": 9092, "nats": 4222, "clickhouse": 8123,
}


@dataclass
class EnvPlan:
    language: str = ""
    base_image: str = ""
    runtime_version: str = ""
    packages: list[str] = field(default_factory=list)
    install: list[str] = field(default_factory=list)
    test_command: str = ""
    log_parser: str = "auto"
    test_file_format: str = "path"
    services: list[dict[str, Any]] = field(default_factory=list)
    env_vars: dict[str, str] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.evidence.append(msg)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ------------------------------------------------------------------ repo files
class RepoView:
    """Read-only view of a repository at one commit (git objects, no checkout)."""

    def __init__(self, repo_dir: str | Path, commit: str):
        self.repo, self.commit = str(repo_dir), commit
        res = subprocess.run(["git", "-C", self.repo, "ls-tree", "-r", "--name-only", "--full-tree", commit],
                             capture_output=True, text=True, encoding="utf-8", errors="replace")
        if res.returncode != 0:
            raise RuntimeError(f"git ls-tree {commit}: {res.stderr.strip()[-200:]}")
        self.paths = set(res.stdout.splitlines())
        self._cache: dict[str, str | None] = {}

    def exists(self, path: str) -> bool:
        return path in self.paths

    def read(self, path: str) -> str | None:
        if path not in self._cache:
            if path not in self.paths:
                self._cache[path] = None
            else:
                r = subprocess.run(["git", "-C", self.repo, "show", f"{self.commit}:{path}"], capture_output=True)
                self._cache[path] = r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None
        return self._cache[path]

    def glob(self, pattern: str) -> list[str]:
        rx = re.compile("^" + re.escape(pattern).replace(r"\*\*/", "(?:.*/)?").replace(r"\*", "[^/]*") + "$")
        return sorted(p for p in self.paths if rx.match(p))


# --------------------------------------------------------------------- versions
def _versions_from(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [v for x in value for v in _versions_from(x)]
    return [m.group(0) for m in re.finditer(r"\d+(?:\.\d+){0,2}", str(value))]


def _vkey(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def _matrix_values(job: dict, key: str) -> list[str]:
    matrix = ((job.get("strategy") or {}).get("matrix") or {})
    vals: list[Any] = []
    if isinstance(matrix, dict):
        vals += matrix.get(key) if isinstance(matrix.get(key), list) else [matrix.get(key)] if matrix.get(key) else []
        for inc in matrix.get("include") or []:
            if isinstance(inc, dict) and inc.get(key) is not None:
                vals.append(inc[key])
    return [str(v) for v in vals if v is not None]


def _resolve(value: Any, job: dict) -> list[str]:
    """A setup-* version input, with ``${{ matrix.x }}`` expanded from the job's matrix."""
    text = str(value or "")
    m = re.search(r"\$\{\{\s*matrix\.([\w-]+)", text)
    if m:
        return [v for v in _matrix_values(job, m.group(1)) if not re.search(r"pypy|graalpy|nightly|dev|alpha|beta|rc", v, re.I)]
    return [text] if text else []


def pick_python(candidates: list[str]) -> str:
    """Newest supported CPython among the candidates (default 3.11)."""
    ok = sorted({".".join(v.split(".")[:2]) for v in candidates if ".".join(v.split(".")[:2]) in PY_SUPPORTED}, key=_vkey)
    return ok[-1] if ok else "3.11"


def _python_from_requires(spec: str) -> list[str]:
    """Versions allowed by a requires-python spec such as '>=3.9,<3.13'."""
    allowed = []
    for v in PY_SUPPORTED:
        good = True
        for clause in spec.split(","):
            m = re.match(r"\s*(>=|<=|==|!=|>|<|~=)\s*(\d+(?:\.\d+)*)", clause)
            if not m:
                continue
            op, ver = m.group(1), _vkey(m.group(2))
            cur = _vkey(v)[:len(ver)] if op in ("==", "!=") else _vkey(v)
            cmp = (cur > ver) - (cur < ver)
            good &= {">=": cmp >= 0, "<=": cmp <= 0, ">": cmp > 0, "<": cmp < 0, "==": cmp == 0,
                     "!=": cmp != 0, "~=": cmp >= 0}[op]
        if good:
            allowed.append(v)
    return allowed


# -------------------------------------------------------------------- workflows
def workflows(view: RepoView) -> list[tuple[str, dict]]:
    out = []
    for path in view.glob(".github/workflows/*.y*ml"):
        try:
            data = loads(view.read(path) or "")
        except (YAMLError, Exception):
            continue
        if isinstance(data, dict):
            out.append((path, data))
    return out


def _test_jobs(wfs: list[tuple[str, dict]]) -> list[tuple[str, str, dict]]:
    """(file, job id, job) for jobs that look like they run tests, test-ish names first."""
    jobs = []
    for path, wf in wfs:
        for jid, job in (wf.get("jobs") or {}).items():
            if not isinstance(job, dict):
                continue
            runs = " ".join(str(s.get("run", "")) for s in job.get("steps") or [] if isinstance(s, dict))
            score = (2 if re.search(r"test", f"{jid} {job.get('name', '')}", re.I) else 0) + \
                    (1 if re.search(r"pytest|tox|nox|npm (run )?test|yarn test|pnpm test|go test|cargo test|"
                                    r"mvn|gradle|rspec|phpunit|make test", runs) else 0)
            if score:
                jobs.append((score, path, str(jid), job))
    return [(p, j, job) for _s, p, j, job in sorted(jobs, key=lambda x: -x[0])]


def _services_from_job(job: dict, plan: EnvPlan, where: str) -> None:
    for name, svc in (job.get("services") or {}).items():
        if not isinstance(svc, dict) or not svc.get("image") or "${{" in str(svc.get("image")):
            continue
        image = str(svc["image"])
        ports = []
        for p in svc.get("ports") or []:
            nums = re.findall(r"\d+", str(p))
            if nums:
                ports.append(int(nums[-1]))
        if not ports:
            ports = [port for frag, port in KNOWN_SERVICES.items() if frag in image.split("/")[-1]][:1]
        env = {str(k): str(v) for k, v in (svc.get("env") or {}).items() if "${{" not in str(v)}
        plan.services.append({"name": re.sub(r"[^a-z0-9-]", "-", str(name).lower()), "image": image,
                              "env": env, "ports": ports})
        plan.note(f"service {name} ({image}) from {where}")


def _compose_services(view: RepoView, plan: EnvPlan) -> None:
    for path in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml",
                 "docker-compose.test.yml", "docker-compose.ci.yml"):
        text = view.read(path)
        if not text:
            continue
        try:
            data = loads(text) or {}
        except Exception:
            continue
        for name, svc in (data.get("services") or {}).items():
            image = str((svc or {}).get("image") or "")
            if not image or not any(frag in image.split("/")[-1].split(":")[0] for frag in KNOWN_SERVICES):
                continue  # only infrastructure images, never the app itself
            if any(s["image"] == image for s in plan.services):
                continue
            env = svc.get("environment") or {}
            if isinstance(env, list):
                env = dict(e.split("=", 1) for e in env if "=" in str(e))
            port = [p for frag, p in KNOWN_SERVICES.items() if frag in image][:1]
            plan.services.append({"name": str(name), "image": image, "env": {str(k): str(v) for k, v in env.items()},
                                  "ports": port})
            plan.note(f"service {name} ({image}) from {path}")


# ----------------------------------------------------------------------- python
_TEST_REQ_FILES = ("requirements/tests.txt", "requirements/test.txt", "requirements/testing.txt",
                   "requirements/dev.txt", "requirements-test.txt", "requirements-tests.txt",
                   "requirements_test.txt", "requirements-dev.txt", "requirements_dev.txt",
                   "test-requirements.txt", "tests/requirements.txt", "requirements.txt")


def _py_toml(text: str) -> dict:
    try:
        import tomllib  # Python 3.11+
    except ImportError:  # pragma: no cover
        return {}
    try:
        return tomllib.loads(text)
    except Exception:
        return {}


def _tox_deps(text: str) -> list[str]:
    """``deps`` of tox's [testenv] (requirement lines and -r files)."""
    m = re.search(r"^\[testenv\]\s*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    if not m:
        return []
    d = re.search(r"^deps\s*=\s*(.*?)(?=^\S|\Z)", m.group(1), re.M | re.S)
    if not d:
        return []
    out = []
    for ln in d.group(1).splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#") and "{" not in ln:
            out.append(ln)
    return out


def python_plan(view: RepoView, plan: EnvPlan, jobs: list) -> None:
    plan.language = "python"
    candidates: list[str] = []
    for path, jid, job in jobs:
        for step in job.get("steps") or []:
            if isinstance(step, dict) and "setup-python" in str(step.get("uses", "")):
                vals = _resolve((step.get("with") or {}).get("python-version"), job)
                if vals:
                    candidates += _versions_from(vals)
                    plan.note(f"python {vals} from {path} job {jid}")
    if not candidates and view.read(".python-version"):
        candidates = _versions_from(view.read(".python-version").splitlines()[0])
        plan.note("python version from .python-version")
    pyproject = _py_toml(view.read("pyproject.toml") or "")
    project = pyproject.get("project") or {}
    if not candidates and project.get("requires-python"):
        candidates = _python_from_requires(str(project["requires-python"]))
        plan.note(f"python from requires-python {project['requires-python']}")
    if not candidates:
        setup = (view.read("setup.py") or "") + (view.read("setup.cfg") or "")
        m = re.search(r"python_requires\s*=\s*['\"]?([^'\"\n]+)", setup)
        if m:
            candidates = _python_from_requires(m.group(1))
            plan.note(f"python from python_requires {m.group(1).strip()}")
    plan.runtime_version = pick_python(candidates)
    plan.base_image = f"python:{plan.runtime_version}-bookworm"
    plan.packages = ["git", "build-essential", "pkg-config"]

    install = ["pip install --no-cache-dir -U pip setuptools wheel pytest"]
    reqs: list[str] = []
    for f in _TEST_REQ_FILES:
        if view.exists(f):
            reqs.append(f)
    for f in view.glob("requirements/*.txt"):
        if re.search(r"test|dev", f) and f not in reqs:
            reqs.append(f)
    if any(re.search(r"test", f) for f in reqs):  # test requirements are enough; dev files pull in docs/lint tools
        reqs = [f for f in reqs if re.search(r"test", f) or f == "requirements.txt"]
    for f in reqs:
        install.append(f"pip install --no-cache-dir -r {shlex.quote(f)} || true")
        plan.note(f"dependencies from {f}")
    tox = view.read("tox.ini") or ""
    for dep in _tox_deps(tox):
        if dep.startswith("-r"):
            f = dep[2:].strip().removeprefix("{toxinidir}/")
            if f not in reqs:
                install.append(f"pip install --no-cache-dir -r {shlex.quote(f)} || true")
        elif not dep.startswith("-"):
            install.append(f"pip install --no-cache-dir {shlex.quote(dep)} || true")
    if _tox_deps(tox):
        plan.note("test dependencies from tox.ini [testenv] deps")
    groups = pyproject.get("dependency-groups") or {}
    for g in ("tests", "test", "testing", "dev"):
        deps = [d for d in groups.get(g, []) if isinstance(d, str)]
        if deps:
            install.append("pip install --no-cache-dir " + " ".join(shlex.quote(d) for d in deps) + " || true")
            plan.note(f"dependency group '{g}' from pyproject.toml")
            break
    extras = [e for e in ("test", "tests", "testing", "dev") if e in (project.get("optional-dependencies") or {})]
    if extras:
        install.append(f"pip install --no-cache-dir -e '.[{extras[0]}]' || pip install --no-cache-dir -e .")
        plan.note(f"extra [{extras[0]}] from pyproject.toml")
    elif view.exists("setup.py") or view.exists("pyproject.toml") or view.exists("setup.cfg"):
        install.append("pip install --no-cache-dir -e . || pip install --no-cache-dir .")
    plan.install = install
    plan.test_command = "python -m pytest -rA --tb=short -p no:cacheprovider {test_files}"
    plan.log_parser, plan.test_file_format = "pytest", "path"


# ------------------------------------------------------------------- javascript
def node_plan(view: RepoView, plan: EnvPlan, jobs: list) -> None:
    pkg = json.loads(view.read("package.json") or "{}")
    plan.language = "typescript" if view.exists("tsconfig.json") else "javascript"
    version = ""
    for f in (".nvmrc", ".node-version"):
        if view.read(f):
            version = (_versions_from(view.read(f).splitlines()[0]) or [""])[0]
            plan.note(f"node {version} from {f}")
            break
    if not version:
        for path, jid, job in jobs:
            for step in job.get("steps") or []:
                if isinstance(step, dict) and "setup-node" in str(step.get("uses", "")):
                    vals = _versions_from(_resolve((step.get("with") or {}).get("node-version"), job))
                    if vals:
                        version = sorted(vals, key=_vkey)[-1]
                        plan.note(f"node {version} from {path} job {jid}")
    if not version and (pkg.get("engines") or {}).get("node"):
        vals = _versions_from(pkg["engines"]["node"])
        version = sorted(vals, key=_vkey)[-1] if vals else ""
        plan.note(f"node from engines.node {pkg['engines']['node']}")
    major = (version.split(".")[0] if version else "20") or "20"
    plan.runtime_version = major
    plan.base_image = f"node:{major}-bookworm"
    plan.packages = ["git", "python3", "build-essential"]
    if view.exists("pnpm-lock.yaml"):
        install = "corepack enable && pnpm install --frozen-lockfile"
    elif view.exists("yarn.lock"):
        install = "corepack enable && (yarn install --frozen-lockfile || yarn install --immutable)"
    elif view.exists("package-lock.json"):
        install = "npm ci"
    else:
        install = "npm install"
    plan.note(f"package manager: {install.split('&&')[-1].split()[0]}")
    plan.install = [install]
    deps = {**(pkg.get("dependencies") or {}), **(pkg.get("devDependencies") or {})}
    if "vitest" in deps:
        plan.test_command, plan.log_parser = "npx vitest run --reporter=json {test_files}", "jest"
    elif "jest" in deps or "ts-jest" in deps:
        plan.test_command, plan.log_parser = "npx jest --ci --json {test_files} 2>/dev/null", "jest"
    elif "mocha" in deps:
        plan.test_command, plan.log_parser = "npx mocha --reporter json {test_files}", "mocha"
    else:
        plan.test_command, plan.log_parser = "npm test -- {test_files}", "auto"
    plan.note(f"test runner from package.json: {plan.test_command.split()[1]}")
    plan.test_file_format = "path"


# ------------------------------------------------------------------- go, rust, …
def go_plan(view: RepoView, plan: EnvPlan, jobs: list) -> None:
    plan.language = "go"
    gomod = view.read("go.mod") or ""
    m = re.search(r"^go\s+(\d+\.\d+)", gomod, re.M)
    plan.runtime_version = m.group(1) if m else "1.22"
    plan.note(f"go {plan.runtime_version} from go.mod" if m else "go version defaulted")
    plan.base_image = f"golang:{plan.runtime_version}-bookworm"
    plan.packages = ["git", "build-essential"]
    plan.install = ["go mod download || true", "go build ./... || true"]
    plan.test_command, plan.log_parser, plan.test_file_format = "go test -json {test_files}", "go", "go-package"


def rust_plan(view: RepoView, plan: EnvPlan, jobs: list) -> None:
    plan.language = "rust"
    chan = ""
    for f in ("rust-toolchain.toml", "rust-toolchain"):
        text = view.read(f)
        if text:
            m = re.search(r'channel\s*=\s*"([^"]+)"', text) or re.match(r"\s*(\S+)", text)
            chan = m.group(1) if m else ""
            plan.note(f"rust {chan} from {f}")
            break
    ver = chan if re.fullmatch(r"\d+\.\d+(\.\d+)?", chan or "") else "1"
    plan.runtime_version, plan.base_image = ver, f"rust:{ver}-bookworm"
    plan.packages = ["git", "pkg-config", "libssl-dev"]
    plan.install = ["cargo fetch", "cargo build --tests || true"]
    plan.test_command, plan.log_parser, plan.test_file_format = "cargo test --no-fail-fast 2>&1", "cargo", "none"


def java_plan(view: RepoView, plan: EnvPlan, jobs: list) -> None:
    plan.language = "java"
    ver = ""
    pom = view.read("pom.xml") or ""
    m = re.search(r"<(?:maven\.compiler\.release|maven\.compiler\.source|java\.version|release)>(\d+)<", pom)
    if m:
        ver = m.group(1)
        plan.note(f"java {ver} from pom.xml")
    for path, jid, job in jobs:
        for step in job.get("steps") or []:
            if not ver and isinstance(step, dict) and "setup-java" in str(step.get("uses", "")):
                vals = _versions_from(_resolve((step.get("with") or {}).get("java-version"), job))
                if vals:
                    ver = sorted(vals, key=_vkey)[-1].split(".")[0]
                    plan.note(f"java {ver} from {path} job {jid}")
    ver = ver if ver and int(ver) >= 8 else "21"
    plan.runtime_version = ver
    if view.exists("pom.xml"):
        plan.base_image = f"maven:3-eclipse-temurin-{ver}"
        plan.install = ["mvn -q -B -DskipTests install || mvn -q -B dependency:go-offline || true"]
        plan.test_command = ("mvn -B -q test -Dsurefire.useFile=true -DfailIfNoTests=false; "
                             "cat target/surefire-reports/*.xml */target/surefire-reports/*.xml 2>/dev/null")
    else:
        plan.base_image = f"gradle:jdk{ver}"
        plan.install = ["gradle --no-daemon testClasses || true"]
        plan.test_command = ("gradle --no-daemon test --continue; "
                             "cat build/test-results/test/*.xml */build/test-results/test/*.xml 2>/dev/null")
    plan.packages = ["git"]
    plan.log_parser, plan.test_file_format = "junit", "none"


# ----------------------------------------------------------------------- detect
def detect(view: RepoView, language: str = "") -> EnvPlan:
    plan = EnvPlan()
    wfs = workflows(view)
    jobs = _test_jobs(wfs)
    if jobs:
        plan.note(f"test job: {jobs[0][1]} in {jobs[0][0]}")
        _services_from_job(jobs[0][2], plan, f"{jobs[0][0]} job {jobs[0][1]}")
        for k, v in (jobs[0][2].get("env") or {}).items():
            if "${{" not in str(v):
                plan.env_vars[str(k)] = str(v)
    _compose_services(view, plan)

    lang = language
    if not lang:
        if view.exists("go.mod"):
            lang = "go"
        elif view.exists("Cargo.toml"):
            lang = "rust"
        elif view.exists("package.json") and not (view.exists("pyproject.toml") or view.exists("setup.py")):
            lang = "javascript"
        elif view.exists("pom.xml") or view.exists("build.gradle") or view.exists("build.gradle.kts"):
            lang = "java"
        elif view.exists("pyproject.toml") or view.exists("setup.py") or view.exists("setup.cfg"):
            lang = "python"
    builders = {"python": python_plan, "javascript": node_plan, "typescript": node_plan, "go": go_plan,
                "rust": rust_plan, "java": java_plan, "kotlin": java_plan}
    if lang in builders:
        builders[lang](view, plan, jobs)
    else:
        plan.language = lang
        plan.note(f"no detector for language {lang!r}; falling back to templates")
    return plan


def detect_for_task(task: Task) -> EnvPlan:
    from .workspace import repo_cache

    repo = repo_cache(task.clone_url, task.base_commit)
    return detect(RepoView(repo, task.base_commit), task.language)


def dockerfile_for(plan: EnvPlan, task: Task, local_bundle: bool) -> str:
    source = "/tmp/repo.bundle" if local_bundle else task.clone_url
    lines = [f"FROM {plan.base_image}", "ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1",
             "RUN (command -v apt-get >/dev/null && apt-get update && apt-get install -y --no-install-recommends "
             f"{' '.join(sorted(set(plan.packages + ['ca-certificates', 'curl', 'git'])))} && rm -rf /var/lib/apt/lists/*) || true"]
    if local_bundle:
        lines.append("COPY repo.bundle /tmp/repo.bundle")
    lines += [f"RUN git clone -q {shlex.quote(source)} /testbed 2>/dev/null; cd /testbed && "
              f"git -c advice.detachedHead=false checkout -q {shlex.quote(task.base_commit)}"
              + (" && rm -f /tmp/repo.bundle" if local_bundle else ""),
              "WORKDIR /testbed"]
    for k, v in plan.env_vars.items():
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", k):
            lines.append(f"ENV {k}={json.dumps(v)}")
    lines += [f"RUN {cmd}" for cmd in plan.install]
    return "\n".join(lines) + "\n"


def apply_plan(task: Task, plan: EnvPlan) -> Task:
    """Write a detected plan into the task's environment (keeps an explicit image untouched)."""
    from .dockerfiles import _is_local

    if not plan.base_image:
        return task
    task.env.dockerfile = dockerfile_for(plan, task, _is_local(task.clone_url))
    task.env.test_command = plan.test_command
    task.env.log_parser = plan.log_parser
    task.env.test_file_format = plan.test_file_format
    task.env.services = list(plan.services)
    task.metadata["env_plan"] = {"evidence": plan.evidence, "base_image": plan.base_image,
                                 "runtime_version": plan.runtime_version}
    return task
