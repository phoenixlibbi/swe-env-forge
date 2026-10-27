"""Repository operations inside a sandbox: checkout, history stripping, patching, test runs."""

from __future__ import annotations

import hashlib
import re
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .hub import cache_dir
from .logparsers import parse_log
from .patches import parse_patch, strip_binary
from .presets import render_test_files
from .sandbox import (DockerSandbox, ExecResult, LocalSandbox, Sandbox, SandboxError, docker_build,
                      docker_image_exists)
from .task import Task

START_MARK, END_MARK = ">>>>> swekit tests start", ">>>>> swekit tests end"
GIT = "git -c core.hooksPath=/dev/null -c advice.detachedHead=false -c safe.directory=*"


class WorkspaceError(RuntimeError):
    pass


def _q(s: str) -> str:
    return shlex.quote(s)


# ---------------------------------------------------------------- repo cache
def repo_cache(url: str, commit: str | None = None) -> Path:
    """A local bare mirror of ``url`` (a URL or a local path), fetched when ``commit`` is missing."""
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", url.rstrip("/").split("://")[-1])[-80:]
    key = f"{key}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.git"
    path = cache_dir() / "repos" / key
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        res = subprocess.run(["git", "clone", "-q", "--mirror", url, str(path)], capture_output=True, text=True)
        if res.returncode != 0:
            raise WorkspaceError(f"git clone {url} failed: {res.stderr.strip()[-500:]}")
    if commit and subprocess.run(["git", "-C", str(path), "cat-file", "-e", f"{commit}^{{commit}}"],
                                 capture_output=True).returncode != 0:
        subprocess.run(["git", "-C", str(path), "fetch", "-q", "--prune", "origin", "+refs/*:refs/*"],
                       capture_output=True)
        if subprocess.run(["git", "-C", str(path), "cat-file", "-e", f"{commit}^{{commit}}"],
                          capture_output=True).returncode != 0:
            subprocess.run(["git", "-C", str(path), "fetch", "-q", "origin", commit], capture_output=True)
    return path


# ------------------------------------------------------------------ sandboxes
def image_tag(task: Task) -> str:
    slug = re.sub(r"[^a-z0-9_.-]+", "-", task.instance_id.lower()).strip("-.")[:100]
    return f"swekit/{slug}:latest"


def ensure_image(task: Task, rebuild: bool = False) -> str:
    """Docker image for ``task``: its prebuilt image, or one built from its Dockerfile."""
    if task.env.image:
        return task.env.image
    tag = image_tag(task)
    if rebuild or not docker_image_exists(tag):
        if task.env.dockerfile:
            docker_build(task.env.dockerfile, tag, task.env.platform)
        else:
            from .dockerfiles import build_default_image

            build_default_image(task, tag)
    return tag


def open_sandbox(task: Task, backend: str = "docker", *, network: bool = True, keep: bool = False,
                 memory: str | None = None) -> Sandbox:
    if backend == "local":
        return LocalSandbox(env_vars=task.env.env_vars, keep=keep)
    if backend == "docker":
        image = ensure_image(task)
        return DockerSandbox(image, task.env.workdir, platform=task.env.platform, network=network,
                             env_vars=task.env.env_vars, keep=keep, memory=memory,
                             pull=not image.startswith("swekit/"))
    raise ValueError(f"unknown backend {backend!r} (docker or local)")


# --------------------------------------------------------------- repo checkout
def checkout_base(sb: Sandbox, task: Task) -> None:
    """Put the repository at ``base_commit`` with a clean tree (cloning it for local sandboxes)."""
    base = _q(task.base_commit)
    if isinstance(sb, LocalSandbox):
        has_repo = (Path(sb.workdir) / ".git").exists()
    else:
        has_repo = sb.exec(f"{GIT} rev-parse --is-inside-work-tree", timeout=60).ok
    if not has_repo:
        if not isinstance(sb, LocalSandbox):
            raise WorkspaceError(f"no git repository at {sb.workdir} in the image")
        mirror = repo_cache(task.clone_url, task.base_commit)
        res = sb.exec(f"git clone -q --no-checkout {_q(str(mirror))} {_q(sb.workdir)}", timeout=1800,
                      workdir=str(Path(sb.workdir).parent))
        if not res.ok:
            raise WorkspaceError(f"clone failed: {res.output[-500:]}")
    res = sb.exec(f"{GIT} checkout -q -f {base} && {GIT} clean -fdq", timeout=600)
    if not res.ok:
        raise WorkspaceError(f"checkout {task.base_commit} failed: {res.output[-500:]}")


def strip_future_history(sb: Sandbox, base_commit: str) -> ExecResult:
    """Delete every ref, remote, stash and reflog entry that could reveal commits after ``base_commit``.

    Afterwards ``git rev-list --all --not <base>`` must be empty, so ``git log --all`` cannot
    show the fix. Tags that point to ancestors of the base are kept.
    """
    b = _q(base_commit)
    script = f"""
set -e
{GIT} checkout -q -f --detach {b}
for r in $({GIT} remote); do {GIT} remote remove "$r"; done
{GIT} for-each-ref --format='%(refname)' | while read -r ref; do
  if ! {GIT} merge-base --is-ancestor "$ref" {b} 2>/dev/null; then {GIT} update-ref -d "$ref"; fi
done
{GIT} stash clear 2>/dev/null || true
{GIT} reflog expire --expire=now --all
{GIT} gc -q --prune=now 2>/dev/null || {GIT} prune --expire=now
left=$({GIT} rev-list --all --not {b} | head -n 1)
if [ -n "$left" ]; then echo "future commit still reachable: $left"; exit 3; fi
echo history-stripped
"""
    res = sb.exec(script, timeout=1800)
    if not res.ok:
        raise WorkspaceError(f"history stripping failed: {res.output[-800:]}")
    return res


def remove_hidden(sb: Sandbox, paths: list[str]) -> None:
    if paths:
        sb.exec("rm -rf " + " ".join(_q(p) for p in paths), timeout=120)


def prepare(sb: Sandbox, task: Task, *, for_agent: bool = False) -> None:
    """Checkout the base commit. For agents also strip history and hidden paths."""
    checkout_base(sb, task)
    if for_agent:
        remove_hidden(sb, task.env.hidden_paths)
        strip_future_history(sb, task.base_commit)


# -------------------------------------------------------------------- patches
@dataclass
class ApplyResult:
    ok: bool
    method: str = ""
    output: str = ""
    skipped_binary: list[str] = field(default_factory=list)


def apply_patch(sb: Sandbox, patch: str, label: str = "patch", *, reset_to: str | None = None) -> ApplyResult:
    """Apply a diff with three fallbacks: ``git apply``, ``git apply --reject``, ``patch --fuzz``.

    ``reset_to`` (a commit) restores a clean tree between attempts; pass it only when this
    is the first patch on the tree, or partial applications would leak into the next try.
    """
    if not patch or not patch.strip():
        return ApplyResult(True, "empty")
    binaries = [f.path for f in parse_patch(patch) if f.binary]
    text = strip_binary(patch) if binaries else patch
    if not text.endswith("\n"):
        text += "\n"
    path = f"{sb.scratch}/{label}.diff"
    sb.write_file(path, text)
    attempts = [("git apply", f"{GIT} apply --verbose --whitespace=nowarn {_q(path)}"),
                ("git apply --reject", f"{GIT} apply --verbose --reject --whitespace=nowarn {_q(path)}"),
                ("patch --fuzz", f"patch --batch --fuzz=5 -p1 -i {_q(path)}")]
    outputs = []
    for i, (name, cmd) in enumerate(attempts):
        if i and reset_to:
            sb.exec(f"{GIT} checkout -q -f {_q(reset_to)} && {GIT} clean -fdq", timeout=600)
        res = sb.exec(cmd, timeout=600)
        outputs.append(f"$ {name}\n{res.output}")
        if res.ok:
            if name != "git apply":
                sb.exec("find . -name '*.rej' -not -path './.git/*' -delete; "
                        "find . -name '*.orig' -not -path './.git/*' -delete", timeout=120)
            return ApplyResult(True, name, "\n".join(outputs), binaries)
        if not reset_to:
            break  # without a reset point, retries could double-apply hunks
    return ApplyResult(False, "", "\n".join(outputs), binaries)


def revert_files(sb: Sandbox, base_commit: str, paths: list[str]) -> None:
    """Restore files to their base version (deleting ones that did not exist at base)."""
    for p in paths:
        qp = _q(p)
        sb.exec(f"if {GIT} cat-file -e {_q(base_commit)}:{qp} 2>/dev/null; then "
                f"{GIT} checkout -q {_q(base_commit)} -- {qp}; else rm -f {qp}; fi", timeout=120)


def collect_diff(sb: Sandbox, base_commit: str, exclude: list[str] | None = None) -> str:
    """The agent's changes as a text diff against ``base_commit`` (untracked files included)."""
    excludes = " ".join(_q(f":(exclude){p}") for p in (exclude or []))
    res = sb.exec(f"{GIT} add -A && {GIT} -c core.fileMode=false diff --cached --no-color --no-ext-diff "
                  f"{_q(base_commit)} -- . {excludes}; rc=$?; {GIT} reset -q; exit $rc", timeout=600)
    if not res.ok:
        raise WorkspaceError(f"git diff failed: {res.output[-500:]}")
    return res.output


# ---------------------------------------------------------------------- tests
@dataclass
class TestRun:
    statuses: dict[str, str]
    log: str
    exit_code: int
    duration: float
    timed_out: bool = False

    @property
    def parsed(self) -> bool:
        return bool(self.statuses)


def _env_lines(task: Task) -> list[str]:
    return [f"export {k}={_q(v)}" for k, v in task.env.env_vars.items()]


def shell_prefix(task: Task, sb: Sandbox) -> list[str]:
    """Lines every command in this task's environment starts with (env vars, setup, cd)."""
    return [*_env_lines(task), *task.env.setup, f"cd {_q(sb.workdir)}"]


def test_script(task: Task, sb: Sandbox) -> str:
    files = task.test_files()
    if task.env.run_script:
        sb.write_file(f"{sb.scratch}/run_script.sh", task.env.run_script)
        sb.write_file(f"{sb.scratch}/parser.py", task.env.parser_script or "")
        rendered = render_test_files(files, task.env.test_file_format)
        s = sb.scratch
        run = (f"bash {s}/run_script.sh {_q(rendered)} > {s}/stdout.log 2> {s}/stderr.log; "
               f"(python3 {s}/parser.py {s}/stdout.log {s}/stderr.log {s}/output.json "
               f"|| python {s}/parser.py {s}/stdout.log {s}/stderr.log {s}/output.json) >/dev/null 2>&1; "
               f"echo; cat {s}/output.json 2>/dev/null; echo")
    elif task.env.test_command:
        run = task.env.test_command.replace("{test_files}", render_test_files(files, task.env.test_file_format))
    else:
        raise WorkspaceError(f"{task.instance_id}: env has neither test_command nor run_script")
    return "\n".join([*shell_prefix(task, sb), *task.env.pre_test, f"echo '{START_MARK}'", run,
                      f"echo '{END_MARK}' $?"])


def run_tests(sb: Sandbox, task: Task, timeout: float | None = None, label: str = "tests") -> TestRun:
    script = test_script(task, sb)
    sb.write_file(f"{sb.scratch}/{label}.sh", script)
    res = sb.exec(f"bash {sb.scratch}/{label}.sh 2>&1", timeout=timeout or task.env.timeout)
    log = res.output
    segment = log
    if START_MARK in log:
        segment = log.split(START_MARK, 1)[1]
        segment = segment.split(END_MARK, 1)[0] if END_MARK in segment else segment
    if task.env.run_script:
        # keep the raw runner output for debugging next to the parsed JSON
        for f in ("stdout.log", "stderr.log"):
            try:
                log += f"\n----- {f} -----\n" + sb.read_file(f"{sb.scratch}/{f}")[-200_000:]
            except SandboxError:
                pass
    statuses = parse_log(segment, task.env.log_parser)
    return TestRun(statuses, log, res.exit_code, res.duration, res.timed_out)
