"""Persistence for projects, project agents and tasks.

Two backends implement one interface:

- `sqlite` — the embedded default, and what the test suite runs on.
- `kds` — KDS (github.com/louischoi0/ckdbs) over its KWP text protocol.

`ProjectService` talks only to `ProjectStore`, so which one is live is a URL.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from ..models import (
    AgentConfig,
    AgentRole,
    ProjectAgentRecord,
    ProjectRecord,
    TaskRecord,
    TaskStatus,
)

log = logging.getLogger("cc_automation.store")


class StoreError(Exception):
    """A constraint the caller broke. Surfaced as 409 or 422."""


class ProjectStore(ABC):
    """What `ProjectService` needs from a database.

    Implementations serialise their own access; callers may assume each method
    is atomic with respect to other calls on the same instance.
    """

    #: short name of the backend, for logs and /health
    backend: str = "abstract"

    @abstractmethod
    def close(self) -> None: ...

    # -- projects ---------------------------------------------------------- #

    @abstractmethod
    def create_project(
        self, project_id: str, name: str, root_dir: Path, tool_policy: list[str]
    ) -> ProjectRecord: ...

    @abstractmethod
    def get_project(self, project_id: str) -> ProjectRecord | None: ...

    @abstractmethod
    def list_projects(self) -> list[ProjectRecord]: ...

    @abstractmethod
    def delete_project(self, project_id: str) -> None: ...

    # -- agents ------------------------------------------------------------ #

    @abstractmethod
    def add_agent(
        self, project_id: str, name: str, role: AgentRole, config: AgentConfig
    ) -> ProjectAgentRecord: ...

    @abstractmethod
    def get_agent(self, project_id: str, name: str) -> ProjectAgentRecord | None: ...

    @abstractmethod
    def get_agent_by_runtime(self, runtime: str) -> ProjectAgentRecord | None: ...

    @abstractmethod
    def list_agents(self, project_id: str | None = None) -> list[ProjectAgentRecord]: ...

    @abstractmethod
    def delete_agent(self, project_id: str, name: str) -> None: ...

    # -- tasks ------------------------------------------------------------- #

    @abstractmethod
    def create_task(
        self,
        task_id: str,
        project_id: str,
        agent: str,
        title: str,
        text: str,
        created_by: str,
        message_id: str,
    ) -> TaskRecord: ...

    @abstractmethod
    def get_task(self, task_id: str) -> TaskRecord | None: ...

    @abstractmethod
    def list_tasks(
        self,
        project_id: str | None = None,
        status: str | None = None,
        agent: str | None = None,
        limit: int = 200,
    ) -> list[TaskRecord]: ...

    @abstractmethod
    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        cost_usd: float | None = None,
    ) -> None: ...

    @abstractmethod
    def cancel_if_queued(self, task_id: str) -> bool: ...

    @abstractmethod
    def cancel_queued_for_agent(self, agent: str, reason: str) -> int: ...

    @abstractmethod
    def fail_running(self, reason: str) -> int: ...


DEFAULT_KDS_PORT = 15432


def open_store(url: str, *, sqlite_path: Path | None = None) -> ProjectStore:
    """Build the store named by `url`.

    - `sqlite:///abs/path.db` or a bare filesystem path
    - `kds://host:port[?fallback=sqlite]`

    `fallback=sqlite` opens the SQLite store when KDS cannot be reached, rather
    than refusing to boot. It is loud on purpose: the two databases do not sync,
    so anything written while the fallback is live is not in KDS.
    """
    parsed = urlparse(url)
    scheme = parsed.scheme or "sqlite"

    if scheme == "sqlite":
        from .sqlite import SqliteProjectStore

        path = _sqlite_path(parsed, url, sqlite_path)
        return SqliteProjectStore(path)

    if scheme == "kds":
        from .kds import KdsProjectStore, KdsUnavailable

        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or DEFAULT_KDS_PORT
        try:
            return KdsProjectStore(host, port)
        except KdsUnavailable as exc:
            if "sqlite" not in parse_qs(parsed.query).get("fallback", []):
                raise StoreError(
                    f"{exc}. Start it, or set CC_AUTOMATION_STORE to a sqlite:// URL"
                ) from exc
            from .sqlite import SqliteProjectStore

            path = sqlite_path or Path("state/projects.db")
            log.error(
                "%s — falling back to sqlite at %s. Anything written now is NOT "
                "in KDS, and the two stores do not sync.", exc, path,
            )
            return SqliteProjectStore(path)

    raise StoreError(f"unsupported store URL scheme {scheme!r}: {url!r}")


def _sqlite_path(parsed, url: str, default: Path | None) -> Path:
    if not parsed.scheme:
        return Path(url).expanduser()
    # sqlite:///abs/path -> netloc "" path "/abs/path"; sqlite://rel/path -> netloc "rel"
    raw = f"{parsed.netloc}{parsed.path}"
    if not raw:
        if default is None:
            raise StoreError(f"sqlite URL names no file: {url!r}")
        return default
    return Path(raw).expanduser()


__all__ = ["ProjectStore", "StoreError", "open_store"]
