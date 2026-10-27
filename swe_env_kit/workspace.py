"""Repository operations inside a sandbox: checkout, history stripping, patching, test runs."""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import shlex
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
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
# safe.directory=* is quoted: an unquoted * would be glob-expanded by files the agent creates.
GIT = "git -c core.hooksPath=/dev/null -c advice.detachedHead=false -c 'safe.directory=*'"
_ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class WorkspaceError(RuntimeError):
    pass


def _q(s: str) -> str:
    return shlex.quote(s)


# ---------------------------------------------------------------- repo cache
@contextlib.contextmanager
def _locked(path: Path):
    """An exclusive lock shared by threads and processes (POSIX flock, thread lock elsewhere)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _THREAD_LOCKS.setdefault(str(path), threading.Lock()):
        with open(f"{path}.lock", "w") as fh:
            try:
                import fcntl

                fcntl.flock(fh, fcntl.LOCK_EX)
            except ImportError:  # pragma: no cover - non-POSIX
                pass
            yield


_THREAD_LOCKS: dict[str, threading.Lock] = {}


def _has_commit(path: Path, commit: str) -> bool:
    return subprocess.run(["git", "-C", str(path), "cat-file", "-e", f"{commit}^{{commit}}"],
                          capture_output=True).returncode == 0


def repo_cache(url: str, commit: str | None = None) -> Path:
    """A local bare mirror of ``url`` (a URL or a local path), fetched when ``commit`` is missing.

    Safe to call from many threads or processes at once.
    """
    key = re.sub(r"[^A-Za-z0-9_.-]+", "_", url.rstrip("/").split("://")[-1])[-80:]
    key = f"{key}-{hashlib.sha1(url.encode()).hexdigest()[:8]}.git"
    path = cache_dir() / "repos" / key
    with _locked(path):
        if not path.exists():
            tmp = path.with_name(f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}")
            shutil.rmtree(tmp, ignore_errors=True)
            res = subprocess.run(["git", "clone", "-q", "--mirror", url, str(tmp)], capture_output=True, text=True)
            if res.returncode != 0:
                shutil.rmtree(tmp, ignore_errors=True)
                raise WorkspaceError(f"git clone {url} failed: {res.stderr.strip()[-500:]}")
            os.replace(tmp, path)
        if commit and not _has_commit(path, commit):
            subprocess.run(["git", "-C", str(path), "fetch", "-q", "--prune", "origin", "+refs/*:refs/*"],
                           capture_output=True)
            if not _has_commit(path, commit):
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
MAX_ENV_COMMITS = 5


def start_ref(sb: Sandbox, task: Task) -> str:
    """The commit this sandbox's repository is reset and diffed against."""
    return getattr(sb, "start_ref", None) or task.base_commit


def _image_head(sb: Sandbox, task: Task) -> str:
    """The image's own HEAD, after checking it is a short descendant of the base (not a future fix)."""
    head = sb.exec(f"{GIT} rev-parse HEAD", timeout=60).output.strip().splitlines()[-1]
    if head.startswith(task.base_commit) or task.base_commit.startswith(head):
        return task.base_commit
    b, h = _q(task.base_commit), _q(head)
    if not sb.exec(f"{GIT} merge-base --is-ancestor {b} {h}", timeout=120).ok:
        raise WorkspaceError(f"image HEAD {head[:12]} does not descend from base {task.base_commit[:12]}")
    ahead = sb.exec(f"{GIT} rev-list --count {b}..{h}", timeout=120).output.strip().splitlines()[-1]
    if not ahead.isdigit() or int(ahead) > MAX_ENV_COMMITS:
        raise WorkspaceError(f"image HEAD is {ahead} commits past the base; refusing to use it as the start "
                             "(it may contain the fix). Use start_ref='base'.")
    return head


def checkout_base(sb: Sandbox, task: Task) -> None:
    """Reset the repository to the task's start commit with a clean tree (cloning it for local sandboxes).

    The start commit is ``base_commit``, or, with ``env.start_ref == "image-head"``, the commit the
    image ships (recorded on first use, so later resets in the same sandbox return to it).
    """
    if isinstance(sb, LocalSandbox):
        has_repo = (Path(sb.workdir) / ".git").exists()
    else:
        has_repo = sb.exec(f"{GIT} rev-parse --is-inside-work-tree", timeout=60).ok
    if has_repo and task.env.start_ref == "image-head" and not getattr(sb, "start_ref", None):
        sb.start_ref = _image_head(sb, task)
    if not has_repo:
        if not isinstance(sb, LocalSandbox):
            raise WorkspaceError(f"no git repository at {sb.workdir} in the image")
        mirror = repo_cache(task.clone_url, task.base_commit)
        res = sb.exec(f"git clone -q --no-checkout {_q(str(mirror))} {_q(sb.workdir)}", timeout=1800,
                      workdir=str(Path(sb.workdir).parent))
        if not res.ok:
            raise WorkspaceError(f"clone failed: {res.output[-500:]}")
    ref = start_ref(sb, task)
    res = sb.exec(f"{GIT} checkout -q -f {_q(ref)} && {GIT} clean -fdq", timeout=600)
    if not res.ok:
        raise WorkspaceError(f"checkout {ref} failed: {res.output[-500:]}")


def strip_future_history(sb: Sandbox, base_commit: str) -> ExecResult:
    """Remove every trace of commits after ``base_commit`` from the repository.

    Deletes refs that are not ancestors of the base (tags on ancestors are kept), remotes,
    stashes, reflogs, ``*_HEAD`` files, ``.keep`` packs and alternates (after copying their
    objects in), then repacks and prunes. Finally checks that every commit object left in
    the store is an ancestor of the base, so neither ``git log --all`` nor ``git show <sha>``
    can reveal the fix.
    """
    b = _q(base_commit)
    script = f"""
set -e
{GIT} checkout -q -f --detach {b}
gitdir=$({GIT} rev-parse --git-dir)
for r in $({GIT} remote); do {GIT} remote remove "$r"; done
{GIT} for-each-ref --format='%(refname)' | while read -r ref; do
  if ! {GIT} merge-base --is-ancestor "$ref" {b} 2>/dev/null; then {GIT} update-ref -d "$ref"; fi
done
{GIT} stash clear 2>/dev/null || true
{GIT} worktree prune 2>/dev/null || true
rm -f "$gitdir"/ORIG_HEAD "$gitdir"/FETCH_HEAD "$gitdir"/MERGE_HEAD "$gitdir"/CHERRY_PICK_HEAD "$gitdir"/REVERT_HEAD
rm -f "$gitdir"/objects/pack/*.keep
{GIT} reflog expire --expire=now --all
{GIT} repack -a -d -q
rm -f "$gitdir"/objects/info/alternates
{GIT} prune --expire=now
{GIT} cat-file --batch-all-objects --batch-check='%(objecttype) %(objectname)' | awk '$1=="commit"{{print $2}}' | sort > "$gitdir"/swekit-all
{GIT} rev-list {b} | sort > "$gitdir"/swekit-anc
left=$(comm -23 "$gitdir"/swekit-all "$gitdir"/swekit-anc | head -n 1)
rm -f "$gitdir"/swekit-all "$gitdir"/swekit-anc
if [ -n "$left" ]; then echo "future commit still in the object store: $left"; exit 3; fi
echo history-stripped
"""
    res = sb.exec(script, timeout=3600)
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
        strip_future_history(sb, start_ref(sb, task))


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
                # remove only the untracked leftovers; tracked *.orig / *.rej files are real content
                sb.exec(f"{GIT} clean -fqx -- '*.rej' '*.orig'", timeout=120)
            return ApplyResult(True, name, "\n".join(outputs), binaries)
        if not reset_to:
            break  # without a reset point, retries could double-apply hunks
    return ApplyResult(False, "", "\n".join(outputs), binaries)


def revert_files(sb: Sandbox, base_commit: str, paths: list[str]) -> list[str]:
    """Restore paths to their base version (removing ones that did not exist at base).

    Returns the paths that could not be restored.
    """
    failed = []
    for p in dict.fromkeys(paths):
        qp, qb = _q(p), _q(base_commit)
        res = sb.exec(f"rm -rf -- {qp}; if {GIT} cat-file -e {qb}:{qp} 2>/dev/null; then "
                      f"{GIT} checkout -q {qb} -- {qp}; fi", timeout=120)
        if not res.ok:
            failed.append(p)
    return failed


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
    for k in task.env.env_vars:
        if not _ENV_KEY.match(k):
            raise WorkspaceError(f"invalid environment variable name {k!r}")
    return [f"export {k}={_q(v)}" for k, v in task.env.env_vars.items()]


def shell_prefix(task: Task, sb: Sandbox) -> list[str]:
    """Lines every command in this task's environment starts with (env vars, setup, cd)."""
    return [*_env_lines(task), *task.env.setup, f"cd {_q(sb.workdir)}"]


def _host_parse(parser_script: str, stdout: str, stderr: str) -> str:
    """Run a trusted Pro ``parser.py`` on the host, away from code under test."""
    with tempfile.TemporaryDirectory(prefix="swekit-parse-") as tmp:
        d = Path(tmp)
        (d / "parser.py").write_text(parser_script, encoding="utf-8")
        (d / "stdout.log").write_text(stdout, encoding="utf-8")
        (d / "stderr.log").write_text(stderr, encoding="utf-8")
        res = subprocess.run([sys.executable, "-I", str(d / "parser.py"), str(d / "stdout.log"),
                              str(d / "stderr.log"), str(d / "output.json")], capture_output=True, text=True,
                             timeout=300, cwd=tmp)
        out = d / "output.json"
        if res.returncode != 0 or not out.exists():
            raise WorkspaceError(f"parser.py failed: {(res.stderr or res.stdout)[-500:]}")
        return out.read_text(encoding="utf-8")


def run_tests(sb: Sandbox, task: Task, timeout: float | None = None, label: str = "tests") -> TestRun:
    """Run the task's tests in ``sb`` and parse the result.

    Each run uses a fresh random directory and random start / end markers, and the script
    deletes itself once started, so code under test cannot easily forge the markers. Pro
    style tasks run ``run_script.sh`` in the sandbox, but their ``parser.py`` runs on the
    host, where the code under test cannot rewrite it.
    """
    nonce = secrets.token_hex(8)
    run_dir = f"{sb.scratch}/run-{nonce}"
    start, end = f"{START_MARK} {nonce}", f"{END_MARK} {nonce}"
    files = task.test_files()
    rendered = render_test_files(files, task.env.test_file_format)
    if task.env.run_script:
        sb.write_file(f"{run_dir}/run_script.sh", task.env.run_script)
        run = (f"bash {_q(run_dir + '/run_script.sh')} {rendered} > {_q(run_dir + '/stdout.log')} "
               f"2> {_q(run_dir + '/stderr.log')}")
    elif task.env.test_command:
        run = task.env.test_command.replace("{test_files}", rendered)
    else:
        raise WorkspaceError(f"{task.instance_id}: env has neither test_command nor run_script")
    script = "\n".join(['rm -f -- "$0"', *shell_prefix(task, sb), *task.env.pre_test, f"echo '{start}'", run,
                        f"echo; echo '{end}' $?"])
    path = f"{run_dir}/{label}.sh"
    sb.write_file(path, script)
    res = sb.exec(f"bash {_q(path)} 2>&1", timeout=timeout or task.env.timeout)
    log = res.output
    if task.env.run_script:
        outs = {}
        for f in ("stdout.log", "stderr.log"):
            try:
                outs[f] = sb.read_file(f"{run_dir}/{f}")
            except SandboxError:
                outs[f] = ""
        try:
            segment = _host_parse(task.env.parser_script or "", outs["stdout.log"], outs["stderr.log"])
        except (WorkspaceError, subprocess.SubprocessError, OSError) as exc:
            segment = ""
            log += f"\n[swekit] {exc}"
        log += f"\n----- stdout.log -----\n{outs['stdout.log'][-200_000:]}\n----- stderr.log -----\n{outs['stderr.log'][-50_000:]}"
    else:
        segment = log.split(start, 1)[1] if start in log else log
        segment = segment.split(end, 1)[0]
    statuses = parse_log(segment, task.env.log_parser)
    return TestRun(statuses, log, res.exit_code, res.duration, res.timed_out)
