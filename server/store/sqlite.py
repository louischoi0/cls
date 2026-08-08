"""SQLite persistence for projects, project agents, and tasks.

README §6 lets the message queue die with the process. Task rows do not get
that licence: a task the manager assigned is a commitment, so it is written
here before it is enqueued and re-enqueued on the next start.

One connection behind one lock. A planning round writes a handful of rows; a
pool would be ceremony around a workload this small.

This is the reference implementation of `ProjectStore`: it enforces every
uniqueness rule in the schema, which is the bar the KDS backend is measured
against.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

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
    utcnow,
)
from . import ProjectStore, StoreError

log = logging.getLogger("cc_automation.store.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    root_dir    TEXT NOT NULL,
    tool_policy TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    github_url  TEXT
);

CREATE TABLE IF NOT EXISTS agents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    runtime_name TEXT NOT NULL UNIQUE,
    role         TEXT NOT NULL CHECK (role IN ('manager', 'worker')),
    config       TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    UNIQUE (project_id, name)
);

-- One projectmanager per project, enforced where it cannot be forgotten.
CREATE UNIQUE INDEX IF NOT EXISTS one_manager_per_project
    ON agents(project_id) WHERE role = 'manager';

-- `agent` and `message_id` are null while status is 'backlog': work that exists
-- but has not been handed to an agent, so nothing is queued for it. They stay
-- null if such a task is cancelled — it was never anyone's. Every other status
-- means a run was dispatched, which requires an agent.
CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent       TEXT,
    title       TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL
                CHECK (status IN
                       ('backlog','queued','running','done','failed','cancelled')),
    created_by  TEXT NOT NULL,
    branch      TEXT,
    milestone_id TEXT,
    message_id  TEXT,
    result      TEXT,
    error       TEXT,
    cost_usd    REAL,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    CHECK (status IN ('backlog', 'cancelled') OR agent IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS tasks_by_project ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS tasks_by_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_by_message ON tasks(message_id);

CREATE TABLE IF NOT EXISTS issues (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('decision','crash','blocker')),
    status      TEXT NOT NULL CHECK (status IN ('open','resolved','dismissed')),
    created_by  TEXT NOT NULL,
    agent       TEXT,
    task_id     TEXT,
    branch      TEXT,
    milestone_id TEXT,
    resolution  TEXT,
    created_at  TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS issues_by_project ON issues(project_id, status);
CREATE INDEX IF NOT EXISTS issues_by_status  ON issues(status);

-- A goal the project is working toward. Tasks hang off it; issues hang off
-- those. Created only by a person (models.MilestoneStatus).
CREATE TABLE IF NOT EXISTS milestones (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    title        TEXT NOT NULL,
    body         TEXT NOT NULL,
    target       TEXT NOT NULL,
    status       TEXT NOT NULL
                 CHECK (status IN ('planned','active','done','abandoned')),
    created_by   TEXT NOT NULL,
    branch       TEXT,
    source       TEXT,
    position     INTEGER NOT NULL DEFAULT 0,
    created_at   TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS milestones_by_project ON milestones(project_id, position);

-- Credentials. Written by the operator, read only when spawning an agent —
-- never returned by any HTTP route, only ever reported as set/not-set.
CREATE TABLE IF NOT EXISTS secrets (
    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    PRIMARY KEY (project_id, key)
);
"""


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class SqliteProjectStore(ProjectStore):
    backend = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA foreign_keys = ON")
        self._db.execute("PRAGMA journal_mode = WAL")
        with self._lock:
            self._db.executescript(SCHEMA)
            self._migrate()
            self._db.commit()

    def _migrate(self) -> None:
        """Bring a database written by an earlier version up to SCHEMA.

        Additive only, and idempotent: a column is added if the table predates
        it. SQLite can do this in place, which is the one place these backends
        genuinely differ — KDS has no ALTER TABLE, so it keeps the same field in
        a side table instead.
        """
        for table, column, decl in (
            ("projects", "github_url", "TEXT"),
            ("tasks", "branch", "TEXT"),
            ("issues", "branch", "TEXT"),
            ("tasks", "milestone_id", "TEXT"),
            ("issues", "milestone_id", "TEXT"),
            ("milestones", "source", "TEXT"),
        ):
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

        # A CHECK cannot be altered in place, and `backlog` is a new one: an
        # older database would refuse every imported task. Rebuilding the table
        # is the only way, so it is done once and detected by the constraint
        # text itself rather than by a version number nothing else needs.
        row = self._db.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'tasks'"
        ).fetchone()
        if row and "'backlog'" not in (row["sql"] or ""):
            self._rebuild_tasks()

    def _rebuild_tasks(self) -> None:
        """Copy `tasks` into a table built from the current SCHEMA.

        Foreign keys are off for the copy, which is what SQLite's own
        twelve-step ALTER recipe prescribes. It also means a row orphaned by an
        older version is carried across rather than turning an upgrade into a
        server that will not start.
        """
        self._db.commit()  # a PRAGMA inside a transaction is silently ignored
        self._db.execute("PRAGMA foreign_keys = OFF")
        try:
            self._db.execute("ALTER TABLE tasks RENAME TO tasks_legacy")
            self._db.executescript(SCHEMA)  # recreates `tasks` with the new CHECKs
            old = {r["name"] for r in self._db.execute("PRAGMA table_info(tasks_legacy)")}
            new = {r["name"] for r in self._db.execute("PRAGMA table_info(tasks)")}
            columns = ", ".join(sorted(old & new))
            self._db.execute(
                f"INSERT INTO tasks ({columns}) SELECT {columns} FROM tasks_legacy"
            )
            # The old indexes were renamed onto tasks_legacy and die with it, so
            # the `IF NOT EXISTS` above skipped them. Recreate them once it is
            # gone.
            self._db.execute("DROP TABLE tasks_legacy")
            self._db.executescript(SCHEMA)
            self._db.commit()
        finally:
            self._db.execute("PRAGMA foreign_keys = ON")
        log.info("tasks table rebuilt: 'backlog' status added")

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- projects ---------------------------------------------------------- #

    def create_project(
        self,
        project_id: str,
        name: str,
        root_dir: Path,
        tool_policy: list[str],
        github_url: str | None = None,
    ) -> ProjectRecord:
        record = ProjectRecord(
            id=project_id,
            name=name,
            root_dir=str(root_dir),
            tool_policy=tool_policy,
            created_at=utcnow(),
            github_url=github_url,
        )
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO projects"
                    " (id, name, root_dir, tool_policy, created_at, github_url)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.name,
                        record.root_dir,
                        json.dumps(record.tool_policy),
                        record.created_at.isoformat(),
                        record.github_url,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise StoreError(f"project {project_id!r} already exists") from exc
            self._db.commit()
        return record

    def get_project(self, project_id: str) -> ProjectRecord | None:
        row = self._db.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        return self._project(row) if row else None

    def list_projects(self) -> list[ProjectRecord]:
        rows = self._db.execute("SELECT * FROM projects ORDER BY id").fetchall()
        return [self._project(r) for r in rows]

    def set_project_github(self, project_id: str, github_url: str | None) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE projects SET github_url = ? WHERE id = ?",
                (github_url, project_id),
            )
            self._db.commit()

    def set_project_tool_policy(self, project_id: str, tool_policy: list[str]) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE projects SET tool_policy = ? WHERE id = ?",
                (json.dumps(tool_policy), project_id),
            )
            self._db.commit()

    def set_secret(self, project_id: str, key: str, value: str | None) -> None:
        with self._lock:
            if value is None:
                self._db.execute(
                    "DELETE FROM secrets WHERE project_id = ? AND key = ?",
                    (project_id, key),
                )
            else:
                self._db.execute(
                    "INSERT INTO secrets (project_id, key, value) VALUES (?, ?, ?)"
                    " ON CONFLICT(project_id, key) DO UPDATE SET value = excluded.value",
                    (project_id, key, value),
                )
            self._db.commit()

    def get_secret(self, project_id: str, key: str) -> str | None:
        row = self._db.execute(
            "SELECT value FROM secrets WHERE project_id = ? AND key = ?",
            (project_id, key),
        ).fetchone()
        return row["value"] if row else None

    def delete_project(self, project_id: str) -> None:
        with self._lock:
            self._db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            self._db.commit()

    @staticmethod
    def _project(row: sqlite3.Row) -> ProjectRecord:
        return ProjectRecord(
            id=row["id"],
            name=row["name"],
            root_dir=row["root_dir"],
            tool_policy=json.loads(row["tool_policy"]),
            created_at=_dt(row["created_at"]),
            github_url=row["github_url"],
        )

    # -- agents ------------------------------------------------------------ #

    def add_agent(
        self, project_id: str, name: str, role: AgentRole, config: AgentConfig
    ) -> ProjectAgentRecord:
        record = ProjectAgentRecord(
            project_id=project_id,
            name=name,
            runtime_name=config.name,
            role=role,
            config=config,
            created_at=utcnow(),
        )
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO agents"
                    " (project_id, name, runtime_name, role, config, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        project_id,
                        name,
                        record.runtime_name,
                        role,
                        config.model_dump_json(),
                        record.created_at.isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                # SQLite names the offending column, not the index, so the
                # message is reconstructed from the rows rather than parsed out
                # of the exception text.
                raise StoreError(
                    self._explain_conflict(project_id, name, record.runtime_name, role)
                ) from exc
            self._db.commit()
        return record

    def _explain_conflict(
        self, project_id: str, name: str, runtime: str, role: AgentRole
    ) -> str:
        if role == "manager" and self._db.execute(
            "SELECT 1 FROM agents WHERE project_id = ? AND role = 'manager'",
            (project_id,),
        ).fetchone():
            return f"project {project_id!r} already has a projectmanager"
        if self._db.execute(
            "SELECT 1 FROM agents WHERE runtime_name = ?", (runtime,)
        ).fetchone():
            return f"runtime agent name {runtime!r} is taken"
        return f"agent {name!r} already exists in project {project_id!r}"

    def update_agent_config(
        self, project_id: str, name: str, config: AgentConfig
    ) -> None:
        with self._lock:
            self._db.execute(
                "UPDATE agents SET config = ? WHERE project_id = ? AND name = ?",
                (config.model_dump_json(), project_id, name),
            )
            self._db.commit()

    def get_agent(self, project_id: str, name: str) -> ProjectAgentRecord | None:
        row = self._db.execute(
            "SELECT * FROM agents WHERE project_id = ? AND name = ?", (project_id, name)
        ).fetchone()
        return self._agent(row) if row else None

    def get_agent_by_runtime(self, runtime: str) -> ProjectAgentRecord | None:
        row = self._db.execute(
            "SELECT * FROM agents WHERE runtime_name = ?", (runtime,)
        ).fetchone()
        return self._agent(row) if row else None

    def list_agents(self, project_id: str | None = None) -> list[ProjectAgentRecord]:
        if project_id is None:
            rows = self._db.execute(
                "SELECT * FROM agents ORDER BY project_id, name"
            ).fetchall()
        else:
            rows = self._db.execute(
                "SELECT * FROM agents WHERE project_id = ? ORDER BY name", (project_id,)
            ).fetchall()
        return [self._agent(r) for r in rows]

    def delete_agent(self, project_id: str, name: str) -> None:
        with self._lock:
            self._db.execute(
                "DELETE FROM agents WHERE project_id = ? AND name = ?",
                (project_id, name),
            )
            self._db.commit()

    @staticmethod
    def _agent(row: sqlite3.Row) -> ProjectAgentRecord:
        return ProjectAgentRecord(
            project_id=row["project_id"],
            name=row["name"],
            runtime_name=row["runtime_name"],
            role=row["role"],
            config=AgentConfig.model_validate_json(row["config"]),
            created_at=_dt(row["created_at"]),
        )

    # -- tasks ------------------------------------------------------------- #

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
        if status != "backlog" and not agent:
            raise StoreError(f"a {status} task must name an agent")
        record = TaskRecord(
            id=task_id,
            project_id=project_id,
            agent=agent,
            title=title,
            text=text,
            status=status,
            created_by=created_by,
            message_id=message_id,
            branch=branch,
            milestone_id=milestone_id,
            created_at=utcnow(),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO tasks"
                " (id, project_id, agent, title, text, status, created_by,"
                "  message_id, branch, milestone_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    project_id,
                    agent,
                    title,
                    text,
                    status,
                    created_by,
                    message_id,
                    branch,
                    milestone_id,
                    record.created_at.isoformat(),
                ),
            )
            self._db.commit()
        return record

    def assign_task(
        self,
        task_id: str,
        agent: str,
        message_id: str,
        branch: str | None = None,
        milestone_id: str | None = None,
    ) -> bool:
        sets = ["agent = ?", "message_id = ?", "status = 'queued'"]
        params: list = [agent, message_id]
        for column, value in (("branch", branch), ("milestone_id", milestone_id)):
            if value is not None:
                sets.append(f"{column} = ?")
                params.append(value)
        params.append(task_id)
        with self._lock:
            cur = self._db.execute(
                f"UPDATE tasks SET {', '.join(sets)}"
                " WHERE id = ? AND status = 'backlog'",
                params,
            )
            self._db.commit()
            return cur.rowcount > 0

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def list_tasks(
        self,
        project_id: str | None = None,
        status: str | None = None,
        agent: str | None = None,
        limit: int = 200,
        branch: str | None = None,
        milestone_id: str | None = None,
    ) -> list[TaskRecord]:
        where, params = [], []
        for column, value in (("project_id", project_id), ("status", status),
                              ("agent", agent), ("branch", branch),
                              ("milestone_id", milestone_id)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        rows = self._db.execute(
            f"SELECT * FROM tasks{clause} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._task(r) for r in rows]

    def set_task_status(
        self,
        task_id: str,
        status: TaskStatus,
        *,
        result: str | None = None,
        error: str | None = None,
        cost_usd: float | None = None,
    ) -> None:
        now = utcnow().isoformat()
        fields = ["status = ?"]
        params: list = [status]
        if status == "running":
            fields.append("started_at = ?")
            params.append(now)
        elif status in ("done", "failed", "cancelled"):
            fields.append("finished_at = ?")
            params.append(now)
        if result is not None:
            fields.append("result = ?")
            params.append(result)
        if error is not None:
            fields.append("error = ?")
            params.append(error[:4000])
        if cost_usd is not None:
            fields.append("cost_usd = ?")
            params.append(cost_usd)
        params.append(task_id)
        with self._lock:
            self._db.execute(
                f"UPDATE tasks SET {', '.join(fields)} WHERE id = ?", params
            )
            self._db.commit()

    def cancel_if_queued(self, task_id: str) -> bool:
        """Cancel work that has not started. Returns whether the row moved.

        `backlog` counts: unassigned work is the easiest kind to drop, and a
        document import that pulled in something useless has to be undoable.
        """
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status = 'cancelled', finished_at = ?"
                " WHERE id = ? AND status IN ('queued', 'backlog')",
                (utcnow().isoformat(), task_id),
            )
            self._db.commit()
            return cur.rowcount > 0

    def cancel_queued_for_agent(self, agent: str, reason: str) -> int:
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status = 'cancelled', finished_at = ?, error = ?"
                " WHERE agent = ? AND status = 'queued'",
                (utcnow().isoformat(), reason, agent),
            )
            self._db.commit()
            return cur.rowcount

    def fail_running(self, reason: str) -> int:
        """Startup sweep: a `running` row means the subprocess died with us."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status = 'failed', finished_at = ?, error = ?"
                " WHERE status = 'running'",
                (utcnow().isoformat(), reason),
            )
            self._db.commit()
            return cur.rowcount

    @staticmethod
    def _task(row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            id=row["id"],
            project_id=row["project_id"],
            agent=row["agent"],
            title=row["title"],
            text=row["text"],
            status=row["status"],
            created_by=row["created_by"],
            branch=row["branch"],
            milestone_id=row["milestone_id"],
            message_id=row["message_id"],
            result=row["result"],
            error=row["error"],
            cost_usd=row["cost_usd"],
            created_at=_dt(row["created_at"]),
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
        )

    # -- issues ------------------------------------------------------------ #

    def create_issue(
        self, issue_id: str, project_id: str, title: str, body: str,
        kind: IssueKind, created_by: str, agent: str | None = None,
        task_id: str | None = None, branch: str | None = None,
        milestone_id: str | None = None,
    ) -> IssueRecord:
        record = IssueRecord(
            id=issue_id, project_id=project_id, title=title, body=body,
            kind=kind, status="open", created_by=created_by, agent=agent,
            task_id=task_id, branch=branch, milestone_id=milestone_id,
            created_at=utcnow(),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO issues"
                " (id, project_id, title, body, kind, status, created_by,"
                "  agent, task_id, branch, milestone_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?)",
                (issue_id, project_id, title, body, kind, created_by, agent,
                 task_id, branch, milestone_id, record.created_at.isoformat()),
            )
            self._db.commit()
        return record

    def get_issue(self, issue_id: str) -> IssueRecord | None:
        row = self._db.execute(
            "SELECT * FROM issues WHERE id = ?", (issue_id,)
        ).fetchone()
        return self._issue(row) if row else None

    def list_issues(
        self, project_id: str | None = None, status: str | None = None,
        kind: str | None = None, limit: int = 200,
        agent: str | None = None, branch: str | None = None,
        milestone_id: str | None = None,
    ) -> list[IssueRecord]:
        where, params = [], []
        for column, value in (("project_id", project_id), ("status", status),
                              ("kind", kind), ("agent", agent), ("branch", branch),
                              ("milestone_id", milestone_id)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        params.append(limit)
        rows = self._db.execute(
            f"SELECT * FROM issues{clause} ORDER BY created_at DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._issue(r) for r in rows]

    def resolve_issue(
        self, issue_id: str, status: IssueStatus, resolution: str
    ) -> bool:
        with self._lock:
            cur = self._db.execute(
                "UPDATE issues SET status = ?, resolution = ?, resolved_at = ?"
                " WHERE id = ? AND status = 'open'",
                (status, resolution, utcnow().isoformat(), issue_id),
            )
            self._db.commit()
            return cur.rowcount > 0

    @staticmethod
    def _issue(row: sqlite3.Row) -> IssueRecord:
        return IssueRecord(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            body=row["body"],
            kind=row["kind"],
            status=row["status"],
            created_by=row["created_by"],
            agent=row["agent"],
            task_id=row["task_id"],
            branch=row["branch"],
            milestone_id=row["milestone_id"],
            resolution=row["resolution"],
            created_at=_dt(row["created_at"]),
            resolved_at=_dt(row["resolved_at"]),
        )

    # -- milestones -------------------------------------------------------- #

    def create_milestone(
        self, milestone_id: str, project_id: str, title: str, body: str,
        target: str, branch: str | None = None, position: int = 0,
        source: str | None = None,
    ) -> MilestoneRecord:
        record = MilestoneRecord(
            id=milestone_id, project_id=project_id, title=title, body=body,
            target=target, status="planned", created_by="user", branch=branch,
            position=position, source=source, created_at=utcnow(),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO milestones"
                " (id, project_id, title, body, target, status, created_by,"
                "  branch, source, position, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'planned', 'user', ?, ?, ?, ?)",
                (milestone_id, project_id, title, body, target, branch, source,
                 position, record.created_at.isoformat()),
            )
            self._db.commit()
        return record

    def get_milestone(self, milestone_id: str) -> MilestoneRecord | None:
        row = self._db.execute(
            "SELECT * FROM milestones WHERE id = ?", (milestone_id,)
        ).fetchone()
        return self._milestone(row) if row else None

    def list_milestones(
        self, project_id: str | None = None, status: str | None = None
    ) -> list[MilestoneRecord]:
        where, params = [], []
        for column, value in (("project_id", project_id), ("status", status)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = self._db.execute(
            f"SELECT * FROM milestones{clause} ORDER BY position, created_at", params
        ).fetchall()
        return [self._milestone(r) for r in rows]

    def update_milestone(self, milestone_id: str, **fields) -> bool:
        allowed = {"title", "body", "target", "branch", "status", "position"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        if not sets:
            return self.get_milestone(milestone_id) is not None
        # Reaching a terminal status stamps the time; leaving one clears it, so
        # a reopened milestone does not keep claiming it finished.
        if "status" in sets:
            sets["completed_at"] = (
                utcnow().isoformat() if sets["status"] in ("done", "abandoned") else None
            )
        assignments = ", ".join(f"{k} = ?" for k in sets)
        with self._lock:
            cur = self._db.execute(
                f"UPDATE milestones SET {assignments} WHERE id = ?",
                [*sets.values(), milestone_id],
            )
            self._db.commit()
            return cur.rowcount > 0

    def delete_milestone(self, milestone_id: str) -> None:
        with self._lock:
            # Tasks and issues outlive their milestone rather than vanishing
            # with it: the work happened, whatever became of the goal.
            for table in ("tasks", "issues"):
                self._db.execute(
                    f"UPDATE {table} SET milestone_id = NULL WHERE milestone_id = ?",
                    (milestone_id,),
                )
            self._db.execute("DELETE FROM milestones WHERE id = ?", (milestone_id,))
            self._db.commit()

    @staticmethod
    def _milestone(row: sqlite3.Row) -> MilestoneRecord:
        return MilestoneRecord(
            id=row["id"],
            project_id=row["project_id"],
            title=row["title"],
            body=row["body"],
            target=row["target"],
            status=row["status"],
            created_by=row["created_by"],
            branch=row["branch"],
            source=row["source"],
            position=row["position"],
            created_at=_dt(row["created_at"]),
            completed_at=_dt(row["completed_at"]),
        )
