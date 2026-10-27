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
        f"RUN git clone -q {shlex.quote(source)} {WORKDIR} && cd {WORKDIR} && "
        f"git -c advice.detachedHead=false checkout -q {shlex.quote(task.base_commit)}",
        f"WORKDIR {WORKDIR}",
        *[f"RUN {cmd}" for cmd in tpl["install"]],
    ]
    return "\n".join(lines) + "\n"


def build_default_image(task: Task, tag: str) -> str:
    """Build the generated Dockerfile for ``task`` as ``tag``; returns the build log."""
    dockerfile = generate_dockerfile(task)
    with tempfile.TemporaryDirectory(prefix="swekit-ctx-") as ctx:
        if _is_local(task.clone_url):
            src = task.clone_url.removeprefix("file://")
            res = subprocess.run(["git", "-C", src, "bundle", "create", str(Path(ctx) / "repo.bundle"), "--all"],
                                 capture_output=True, text=True)
            if res.returncode != 0:
                raise RuntimeError(f"git bundle failed: {res.stderr.strip()[-300:]}")
        return docker_build(dockerfile, tag, task.env.platform, context=ctx)
