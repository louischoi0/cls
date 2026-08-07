import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.models import AgentConfig  # noqa: E402
from server.registry import Registry  # noqa: E402


def make_agent(name: str, tags: list[str], cwd: Path, **kw) -> AgentConfig:
    return AgentConfig(name=name, tags=tags, cwd=cwd, **kw)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def registry(workdir: Path) -> Registry:
    return Registry(
        [
            make_agent("alpha", ["research", "shared"], workdir),
            make_agent("beta", ["ops", "shared"], workdir),
        ]
    )
