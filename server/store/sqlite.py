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
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from ..models import (
    AgentConfig,
    AgentRole,
    ProjectAgentRecord,
    ProjectRecord,
    TaskRecord,
    TaskStatus,
    utcnow,
)
from . import ProjectStore, StoreError

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    root_dir    TEXT NOT NULL,
    tool_policy TEXT NOT NULL,
    created_at  TEXT NOT NULL
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

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    project_id  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    agent       TEXT NOT NULL,
    title       TEXT NOT NULL,
    text        TEXT NOT NULL,
    status      TEXT NOT NULL
                CHECK (status IN ('queued','running','done','failed','cancelled')),
    created_by  TEXT NOT NULL,
    message_id  TEXT,
    result      TEXT,
    error       TEXT,
    cost_usd    REAL,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);

CREATE INDEX IF NOT EXISTS tasks_by_project ON tasks(project_id, status);
CREATE INDEX IF NOT EXISTS tasks_by_status  ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_by_message ON tasks(message_id);
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
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- projects ---------------------------------------------------------- #

    def create_project(
        self, project_id: str, name: str, root_dir: Path, tool_policy: list[str]
    ) -> ProjectRecord:
        record = ProjectRecord(
            id=project_id,
            name=name,
            root_dir=str(root_dir),
            tool_policy=tool_policy,
            created_at=utcnow(),
        )
        with self._lock:
            try:
                self._db.execute(
                    "INSERT INTO projects (id, name, root_dir, tool_policy, created_at)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (
                        record.id,
                        record.name,
                        record.root_dir,
                        json.dumps(record.tool_policy),
                        record.created_at.isoformat(),
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
        agent: str,
        title: str,
        text: str,
        created_by: str,
        message_id: str,
    ) -> TaskRecord:
        record = TaskRecord(
            id=task_id,
            project_id=project_id,
            agent=agent,
            title=title,
            text=text,
            status="queued",
            created_by=created_by,
            message_id=message_id,
            created_at=utcnow(),
        )
        with self._lock:
            self._db.execute(
                "INSERT INTO tasks"
                " (id, project_id, agent, title, text, status, created_by,"
                "  message_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'queued', ?, ?, ?)",
                (
                    task_id,
                    project_id,
                    agent,
                    title,
                    text,
                    created_by,
                    message_id,
                    record.created_at.isoformat(),
                ),
            )
            self._db.commit()
        return record

    def get_task(self, task_id: str) -> TaskRecord | None:
        row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._task(row) if row else None

    def list_tasks(
        self,
        project_id: str | None = None,
        status: str | None = None,
        agent: str | None = None,
        limit: int = 200,
    ) -> list[TaskRecord]:
        where, params = [], []
        if project_id:
            where.append("project_id = ?")
            params.append(project_id)
        if status:
            where.append("status = ?")
            params.append(status)
        if agent:
            where.append("agent = ?")
            params.append(agent)
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
        """Cancel only from `queued`. Returns whether the row moved."""
        with self._lock:
            cur = self._db.execute(
                "UPDATE tasks SET status = 'cancelled', finished_at = ?"
                " WHERE id = ? AND status = 'queued'",
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
            message_id=row["message_id"],
            result=row["result"],
            error=row["error"],
            cost_usd=row["cost_usd"],
            created_at=_dt(row["created_at"]),
            started_at=_dt(row["started_at"]),
            finished_at=_dt(row["finished_at"]),
        )
