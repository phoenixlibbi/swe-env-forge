"""Execution sandboxes: Docker containers or local temporary checkouts, one interface."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ExecResult:
    exit_code: int
    output: str
    duration: float
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out


class SandboxError(RuntimeError):
    pass


class Sandbox(ABC):
    """A place to run shell commands against a repository checkout."""

    workdir: str   # repository root inside the sandbox
    scratch: str   # private directory for harness files (never inside the repo)

    @abstractmethod
    def exec(self, cmd: str, timeout: float | None = None, workdir: str | None = None,
             env: dict[str, str] | None = None) -> ExecResult: ...

    @abstractmethod
    def write_file(self, path: str, content: str | bytes) -> None: ...

    @abstractmethod
    def read_file(self, path: str) -> str: ...

    @abstractmethod
    def close(self) -> None: ...

    def __enter__(self) -> "Sandbox":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _abs(self, path: str) -> str:
        return path if path.startswith("/") else f"{self.workdir.rstrip('/')}/{path}"


# ----------------------------------------------------------------------- docker
def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def docker_image_exists(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


def docker_pull(image: str, platform: str | None = None, timeout: float = 3600) -> None:
    if docker_image_exists(image):
        return
    cmd = ["docker", "pull", "-q", *(["--platform", platform] if platform else []), image]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise SandboxError(f"docker pull {image} failed: {res.stderr.strip()[-500:]}")


def docker_build(dockerfile: str, tag: str, platform: str | None = None, context: str | None = None,
                 timeout: float = 7200) -> str:
    """Build ``dockerfile`` (text) as ``tag``; returns the build log."""
    with tempfile.TemporaryDirectory(prefix="swekit-build-") as tmp:
        ctx = context or tmp
        df = Path(tmp) / "Dockerfile"
        df.write_text(dockerfile, encoding="utf-8")
        cmd = ["docker", "build", "-t", tag, "-f", str(df), *(["--platform", platform] if platform else []), ctx]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    log = res.stdout + res.stderr
    if res.returncode != 0:
        raise SandboxError(f"docker build {tag} failed:\n{log[-3000:]}")
    return log


class DockerSandbox(Sandbox):
    """A long-lived container; every command runs through ``docker exec``."""

    def __init__(self, image: str, workdir: str = "/testbed", *, platform: str | None = None,
                 network: bool = True, env_vars: dict[str, str] | None = None, memory: str | None = None,
                 cpus: str | None = None, keep: bool = False, pull: bool = True):
        self.image, self.workdir, self.keep = image, workdir, keep
        self.scratch = "/tmp/swekit"
        self.env_vars = dict(env_vars or {})
        self.name = f"swekit-{uuid.uuid4().hex[:12]}"
        if pull:
            docker_pull(image, platform)
        cmd = ["docker", "run", "-d", "--name", self.name, "--entrypoint", "tail",
               *(["--platform", platform] if platform else []),
               *([] if network else ["--network", "none"]),
               *(["--memory", memory] if memory else []), *(["--cpus", cpus] if cpus else []),
               image, "-f", "/dev/null"]
        self._closed = False
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
            if res.returncode != 0:
                raise SandboxError(f"docker run {image} failed: {res.stderr.strip()[-500:]}")
            probe = self._raw(["sh", "-c", f"mkdir -p {self.scratch}; command -v bash; command -v timeout"], 120)
        except BaseException:
            subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=120)
            self._closed = True
            raise
        self.shell = "bash" if "bash" in probe.output else "sh"
        self.has_timeout = "timeout" in probe.output.replace("bash", "")

    def _raw(self, argv: list[str], timeout: float | None, workdir: str | None = None,
             env: dict[str, str] | None = None, stdin: bytes | None = None) -> ExecResult:
        envs = {**self.env_vars, **(env or {})}
        cmd = ["docker", "exec", *(["-i"] if stdin is not None else []),
               *[a for k, v in envs.items() for a in ("-e", f"{k}={v}")],
               *(["-w", workdir] if workdir else []), self.name, *argv]
        start = time.monotonic()
        try:
            res = subprocess.run(cmd, input=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=timeout)
            return ExecResult(res.returncode, res.stdout.decode("utf-8", "replace"), time.monotonic() - start)
        except subprocess.TimeoutExpired as exc:
            out = (exc.stdout or b"").decode("utf-8", "replace")
            return ExecResult(124, out, time.monotonic() - start, timed_out=True)

    def exec(self, cmd: str, timeout: float | None = None, workdir: str | None = None,
             env: dict[str, str] | None = None) -> ExecResult:
        argv = [self.shell, "-c", cmd]
        backstop = None
        if timeout:
            if self.has_timeout:
                argv = ["timeout", "-s", "KILL", str(int(max(1, timeout))), *argv]
            backstop = timeout + 30
        res = self._raw(argv, backstop, workdir or self.workdir, env)
        if timeout and res.exit_code in (124, 137) and res.duration >= timeout - 1:
            res.timed_out = True
        return res

    def write_file(self, path: str, content: str | bytes) -> None:
        data = content.encode("utf-8") if isinstance(content, str) else content
        p = shlex.quote(self._abs(path))
        res = self._raw(["sh", "-c", f'mkdir -p "$(dirname {p})" && cat > {p}'], 120, stdin=data)
        if res.exit_code != 0:
            raise SandboxError(f"write {path} failed: {res.output[-300:]}")

    def read_file(self, path: str) -> str:
        res = self._raw(["cat", self._abs(path)], 120)
        if res.exit_code != 0:
            raise SandboxError(f"read {path} failed: {res.output[-300:]}")
        return res.output

    def close(self) -> None:
        if getattr(self, "_closed", True) or self.keep:
            return
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True, timeout=120)
        self._closed = True


# ------------------------------------------------------------------------ local
class LocalSandbox(Sandbox):
    """Runs commands on the host inside a temporary directory.

    Fast and dependency-free, used by tests, CI and the demo. It offers no isolation:
    only use it for code you trust.
    """

    def __init__(self, root: str | None = None, env_vars: dict[str, str] | None = None, keep: bool = False):
        self.root = Path(root or tempfile.mkdtemp(prefix="swekit-local-")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.workdir = str(self.root / "repo")
        self.scratch = str(self.root / "scratch")
        Path(self.scratch).mkdir(exist_ok=True)
        self.env_vars, self.keep = dict(env_vars or {}), keep
        self.shell = "bash" if shutil.which("bash") else "sh"

    def exec(self, cmd: str, timeout: float | None = None, workdir: str | None = None,
             env: dict[str, str] | None = None) -> ExecResult:
        cwd = workdir or self.workdir
        if not Path(cwd).is_dir():
            cwd = str(self.root)
        path = os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "")])
        full_env = {**os.environ, "PATH": path, **self.env_vars, **(env or {}), "GIT_TERMINAL_PROMPT": "0"}
        start = time.monotonic()
        proc = subprocess.Popen([self.shell, "-c", cmd], cwd=cwd, env=full_env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = proc.communicate(timeout=timeout)
            return ExecResult(proc.returncode, out.decode("utf-8", "replace"), time.monotonic() - start)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            out, _ = proc.communicate()
            return ExecResult(124, out.decode("utf-8", "replace"), time.monotonic() - start, timed_out=True)

    def _host(self, path: str) -> Path:
        p = Path(self._abs(path))
        return p if p.is_absolute() else self.root / p

    def write_file(self, path: str, content: str | bytes) -> None:
        p = self._host(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content.encode("utf-8") if isinstance(content, str) else content)

    def read_file(self, path: str) -> str:
        try:
            return self._host(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise SandboxError(f"read {path} failed: {exc}") from None

    def close(self) -> None:
        if not self.keep:
            shutil.rmtree(self.root, ignore_errors=True)
