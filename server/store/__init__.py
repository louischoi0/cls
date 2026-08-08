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
    IssueKind,
    IssueRecord,
    IssueStatus,
    MilestoneRecord,
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
        self,
        project_id: str,
        name: str,
        root_dir: Path,
        tool_policy: list[str],
        github_url: str | None = None,
    ) -> ProjectRecord: ...

    @abstractmethod
    def set_project_github(self, project_id: str, github_url: str | None) -> None:
        """Set or clear the repository link. Unknown project is a no-op."""
        ...

    @abstractmethod
    def set_project_tool_policy(self, project_id: str, tool_policy: list[str]) -> None:
        """Replace the ceiling on what this project's agents may be granted.

        Widening it does not retroactively grant anything: an agent's own
        `allowed_tools` was resolved when it was created. Unknown project is a
        no-op.
        """
        ...

    @abstractmethod
    def set_secret(self, project_id: str, key: str, value: str | None) -> None:
        """Store a credential. Never read back through any HTTP route."""
        ...

    @abstractmethod
    def get_secret(self, project_id: str, key: str) -> str | None: ...

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
    def update_agent_config(
        self, project_id: str, name: str, config: AgentConfig
    ) -> None:
        """Replace an agent's stored settings. Its name and role do not change,
        so nothing it is keyed by moves. Unknown agent is a no-op."""
        ...

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
        agent: str | None,
        title: str,
        text: str,
        created_by: str,
        message_id: str | None,
        branch: str | None = None,
        milestone_id: str | None = None,
        status: TaskStatus = "queued",
    ) -> TaskRecord:
        """Write a task row.

        `status="backlog"` is the one case where `agent` and `message_id` may be
        None: the work exists but has not been handed to anyone, so there is no
        queue entry for it to correlate with.
        """
        ...

    @abstractmethod
    def assign_task(
        self,
        task_id: str,
        agent: str,
        message_id: str,
        branch: str | None = None,
        milestone_id: str | None = None,
    ) -> bool:
        """Move a task from `backlog` to `queued` under `agent`.

        Compare-and-set on the status, so two callers racing to assign the same
        backlog task cannot both enqueue it. Returns whether this call won.
        `branch` and `milestone_id` are set when given and left alone when None,
        in the same statement — an assignment is one decision, not three.
        """
        ...

    @abstractmethod
    def get_task(self, task_id: str) -> TaskRecord | None: ...

    @abstractmethod
    def list_tasks(
        self,
        project_id: str | None = None,
        status: str | None = None,
        agent: str | None = None,
        limit: int = 200,
        branch: str | None = None,
        milestone_id: str | None = None,
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

    # -- issues ------------------------------------------------------------ #

    @abstractmethod
    def create_issue(
        self,
        issue_id: str,
        project_id: str,
        title: str,
        body: str,
        kind: IssueKind,
        created_by: str,
        agent: str | None = None,
        task_id: str | None = None,
        branch: str | None = None,
        milestone_id: str | None = None,
    ) -> IssueRecord: ...

    @abstractmethod
    def get_issue(self, issue_id: str) -> IssueRecord | None: ...

    @abstractmethod
    def list_issues(
        self,
        project_id: str | None = None,
        status: str | None = None,
        kind: str | None = None,
        limit: int = 200,
        agent: str | None = None,
        branch: str | None = None,
        milestone_id: str | None = None,
    ) -> list[IssueRecord]: ...

    @abstractmethod
    def resolve_issue(
        self, issue_id: str, status: IssueStatus, resolution: str
    ) -> bool:
        """Close an issue. Returns False unless it moved from `open`."""
        ...

    # -- milestones -------------------------------------------------------- #
    # Created by a person, never by an agent: see models.MilestoneStatus.

    @abstractmethod
    def create_milestone(
        self,
        milestone_id: str,
        project_id: str,
        title: str,
        body: str,
        target: str,
        branch: str | None = None,
        position: int = 0,
        source: str | None = None,
    ) -> MilestoneRecord: ...

    @abstractmethod
    def get_milestone(self, milestone_id: str) -> MilestoneRecord | None: ...

    @abstractmethod
    def list_milestones(
        self, project_id: str | None = None, status: str | None = None
    ) -> list[MilestoneRecord]: ...

    @abstractmethod
    def update_milestone(self, milestone_id: str, **fields) -> bool:
        """Patch a milestone. Returns whether the row existed."""
        ...

    @abstractmethod
    def delete_milestone(self, milestone_id: str) -> None:
        """Remove a milestone; its tasks and issues survive, unparented."""
        ...


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
