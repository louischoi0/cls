import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.models import SessionConfig  # noqa: E402
from server.registry import Registry  # noqa: E402

#: Every test that builds an app uses this, so a key is never read off the box.
KEY = "test-key-abcdefghijklmnop"
AUTH = {"X-API-Key": KEY}


def make_session(name: str, cwd: Path, **kw) -> SessionConfig:
    return SessionConfig(name=name, cwd=cwd, **kw)


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    d = tmp_path / "work"
    d.mkdir()
    return d


@pytest.fixture
def registry(workdir: Path) -> Registry:
    return Registry([make_session("alpha", workdir), make_session("beta", workdir)])
