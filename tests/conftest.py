import os
import tempfile
from pathlib import Path

import pytest

from swe_env_forge.demo import build_demo_repo
from swe_env_forge.mine import MineConfig, mine_commits

FLAKY_COUNTER = Path(tempfile.gettempdir()) / "minishop-flaky-counter"


@pytest.fixture(scope="session", autouse=True)
def _isolated_cache(tmp_path_factory):
    os.environ["SWEFORGE_CACHE"] = str(tmp_path_factory.mktemp("sweforge-cache"))


@pytest.fixture(scope="session")
def demo_repo(tmp_path_factory):
    path = tmp_path_factory.mktemp("demo") / "minishop"
    shas = build_demo_repo(path)
    return path, shas


@pytest.fixture(scope="session")
def demo_tasks(demo_repo):
    path, shas = demo_repo
    tasks = mine_commits(str(path), repo="demo/minishop", cfg=MineConfig())
    by_commit = {t.metadata["instance_commit"]: t for t in tasks}
    return {"fix": by_commit[shas[1]], "feature": by_commit[shas[2]], "bulk": by_commit[shas[4]],
            "flaky": by_commit[shas[5]], "all": tasks, "shas": shas}


@pytest.fixture()
def reset_flaky_counter():
    FLAKY_COUNTER.unlink(missing_ok=True)
    yield
    FLAKY_COUNTER.unlink(missing_ok=True)
