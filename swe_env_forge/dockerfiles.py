"""Default Docker environments for tasks that ship no image or Dockerfile (e.g. mined tasks)."""

from __future__ import annotations

import shlex
import subprocess
import tempfile
from pathlib import Path

from .presets import LANGUAGE_TEMPLATES
from .sandbox import docker_build
from .task import Task

WORKDIR = "/testbed"


def _is_local(url: str) -> bool:
    return url.startswith("file://") or Path(url).expanduser().exists()


def generate_dockerfile(task: Task, from_bundle: bool | None = None) -> str:
    """A best-effort Dockerfile: language base image, repo at ``base_commit``, dependencies installed.

    When the repository is local (a path), the build context carries a git bundle
    (``repo.bundle``) instead of cloning from the network.
    """
    tpl = LANGUAGE_TEMPLATES.get(task.language)
    if not tpl:
        raise ValueError(f"{task.instance_id}: no default environment for language {task.language!r}; "
                         "set env.image or env.dockerfile")
    local = _is_local(task.clone_url) if from_bundle is None else from_bundle
    source = "/tmp/repo.bundle" if local else task.clone_url
    lines = [
        f"FROM {tpl['base']}",
        "ENV DEBIAN_FRONTEND=noninteractive",
        "RUN (command -v apt-get >/dev/null && apt-get update && apt-get install -y --no-install-recommends "
        f"{tpl['packages']} ca-certificates curl && rm -rf /var/lib/apt/lists/*) || true",
    ]
    if local:
        lines.append("COPY repo.bundle /tmp/repo.bundle")
    lines += [
        f"RUN git clone -q {shlex.quote(source)} {WORKDIR} 2>/dev/null; cd {WORKDIR} && "
        f"git -c advice.detachedHead=false checkout -q {shlex.quote(task.base_commit)}"
        + (" && rm -f /tmp/repo.bundle" if local else ""),
        f"WORKDIR {WORKDIR}",
        *[f"RUN {cmd}" for cmd in tpl["install"]],
    ]
    return "\n".join(lines) + "\n"


def build_default_image(task: Task, tag: str) -> str:
    """Build the generated Dockerfile for ``task`` as ``tag``; returns the build log."""
    dockerfile = generate_dockerfile(task)
    with tempfile.TemporaryDirectory(prefix="sweforge-ctx-") as ctx:
        if _is_local(task.clone_url):
            # Bundle only the base commit's history: a bundle of --all would ship the fix.
            from .workspace import _locked, repo_cache

            mirror = repo_cache(task.clone_url, task.base_commit)
            ref = f"refs/heads/sweforge-base-{task.base_commit[:12]}"
            with _locked(mirror):
                subprocess.run(["git", "-C", str(mirror), "update-ref", ref, task.base_commit], check=True)
                try:
                    res = subprocess.run(["git", "-C", str(mirror), "bundle", "create", "-q",
                                          str(Path(ctx) / "repo.bundle"), ref], capture_output=True, text=True)
                finally:
                    subprocess.run(["git", "-C", str(mirror), "update-ref", "-d", ref], capture_output=True)
            if res.returncode != 0:
                raise RuntimeError(f"git bundle failed: {res.stderr.strip()[-300:]}")
        return docker_build(dockerfile, tag, task.env.platform, context=ctx)
