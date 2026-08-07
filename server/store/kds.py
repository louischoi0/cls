"""KDS-backed persistence (github.com/louischoi0/ckdbs).

KDS is an OLTP engine with a deliberately narrow surface, and three of its
absences shape this file. They are design positions of that engine, not gaps to
work around quietly, so each is met explicitly here:

**No UNIQUE, and no CREATE INDEX by design.** The SQLite backend enforces
`UNIQUE(project_id, name)`, `UNIQUE(runtime_name)` and one-projectmanager-per-
project in the schema, where they cannot be forgotten. Here they are read-then-
write checks inside a transaction, under the same lock that serialises the
connection. That holds for this server — one process, one connection — and is
strictly weaker: a second writer against the same database could interleave
between the check and the insert. `docs/kds-backend.md` says so plainly.

**No NULL, no escaping, no value over 8144 bytes.** All three are `codec.py`'s
job; nothing here sends a value that has not been through it.

**No ORDER BY, no LIMIT.** Ordering and slicing happen in Python, after the
rows are back.

Every lookup is a scan — the engine has no secondary indexes, on purpose. What
it has instead is Cabins, so the columns this store filters on get one at
schema time.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from contextlib import contextmanager
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
from .codec import BLOB_PREFIX, NULL_CELL, MAX_CELL, blob_id, chunks, decode, encode, is_blob, quote
from .kwp import KwpConnection, KwpError

log = logging.getLogger("cc_automation.kds")

# Column 0 of every relation is the engine's Keystone id: an integer, assigned
# by the server, never supplied by INSERT. `rid` is that column; this store
# addresses rows by its own string ids and only ever reads it back.
SCHEMA = [
    "CREATE TABLE projects (rid int64, id varchar, name varchar, root_dir varchar,"
    " tool_policy varchar, created_at varchar) BTREE",
    "CREATE TABLE agents (rid int64, project_id varchar, name varchar,"
    " runtime_name varchar, role varchar, config varchar, created_at varchar) BTREE",
    # `body` rather than `text`, and `id` rather than a keyword, so the column
    # list never collides with the grammar.
    "CREATE TABLE tasks (rid int64, id varchar, project_id varchar, agent varchar,"
    " title varchar, body varchar, status varchar, created_by varchar,"
    " message_id varchar, result varchar, error varchar, cost_usd varchar,"
    " created_at varchar, started_at varchar, finished_at varchar) BTREE",
    "CREATE TABLE blobs (rid int64, blob_id varchar, seq int64, chunk varchar) BTREE",
]

# The equality columns this store actually filters on. A Cabin is authoritative
# for the values it has observed, which is what replaces the secondary indexes
# the engine does not have.
CABINS = [
    ("projects", "id"),
    ("agents", "project_id"),
    ("agents", "runtime_name"),
    ("tasks", "id"),
    ("tasks", "project_id"),
    ("tasks", "status"),
    ("tasks", "agent"),
    ("blobs", "blob_id"),
]

TASK_COLUMNS = [
    "id", "project_id", "agent", "title", "body", "status", "created_by",
    "message_id", "result", "error", "cost_usd", "created_at", "started_at",
    "finished_at",
]
PROJECT_COLUMNS = ["id", "name", "root_dir", "tool_policy", "created_at"]
AGENT_COLUMNS = ["project_id", "name", "runtime_name", "role", "config", "created_at"]

#: Columns that can hold a value long enough to be chunked into `blobs`.
SPILLABLE = {"tasks": ("title", "body", "result", "error"), "agents": ("config",),
             "projects": ("name", "root_dir", "tool_policy")}


class KdsUnavailable(Exception):
    """kds_server could not be reached. Distinct so the fallback can catch it."""


class KdsProjectStore(ProjectStore):
    backend = "kds"

    def __init__(self, host: str = "127.0.0.1", port: int = 15432) -> None:
        self.host = host
        self.port = port
        self._lock = threading.Lock()
        self._conn = KwpConnection(host, port)
        try:
            self._conn.connect()
            self._conn.command("PING")
        except (KwpError, OSError) as exc:
            raise KdsUnavailable(f"kds_server unreachable at {host}:{port} ({exc})") from exc
        self._init_schema()
        log.info("using KDS at %s:%s", host, port)

    def close(self) -> None:
        with self._lock:
            try:
                # Nothing else makes a CREATE TABLE or UPDATE survive the
                # process dying; only INSERT is logged per statement.
                self._conn.command("SYNC")
            except KwpError as exc:
                log.warning("final SYNC failed: %s", exc)
            self._conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            for ddl in SCHEMA:
                self._conn.command(ddl)  # idempotent: replies EXISTS oid=<n>
            for table, column in CABINS:
                try:
                    self._conn.command(f"CREATE CABIN ON {table}({column})")
                except KwpError as exc:
                    # Already declared is the normal case on every boot but the
                    # first; anything else costs speed, never correctness.
                    log.debug("cabin %s.%s: %s", table, column, exc)
            self._conn.command("SYNC")

    # -- wire helpers ------------------------------------------------------ #

    @contextmanager
    def _txn(self):
        """One transaction. Rolls back on any failure, including ours."""
        self._conn.begin()
        try:
            yield
        except BaseException:
            self._conn.rollback()
            raise
        self._conn.commit()

    def _where(self, **eq: str | None) -> str:
        terms = [f"{col} = {quote(encode(val))}" for col, val in eq.items() if val is not None]
        return f" WHERE {' AND '.join(terms)}" if terms else ""

    def _select(self, table: str, **eq: str | None) -> list[dict[str, str]]:
        return self._conn.select(f"SELECT * FROM {table}{self._where(**eq)}")

    def _insert(self, table: str, cells: list[str | int]) -> None:
        values = ", ".join(str(c) if isinstance(c, int) else quote(c) for c in cells)
        self._conn.command(f"INSERT INTO {table} VALUES ({values})")

    @staticmethod
    def _count(reply: str) -> int:
        """`UPDATED 3` / `DELETED 2` -> 3 / 2."""
        try:
            return int(reply.rsplit(" ", 1)[1])
        except (IndexError, ValueError):
            return 0

    # -- value spilling ---------------------------------------------------- #

    def _put(self, value: str | None) -> str:
        """A Python value -> one cell, chunking into `blobs` when too long."""
        if value is None:
            return NULL_CELL
        body = encode(value)
        if len(body) <= MAX_CELL:
            return body
        bid = uuid.uuid4().hex
        for seq, chunk in enumerate(chunks(body)):
            self._insert("blobs", [bid, seq, chunk])
        return BLOB_PREFIX + bid

    def _get(self, cell: str) -> str | None:
        if cell == NULL_CELL:
            return None
        if not is_blob(cell):
            return decode(cell)
        rows = self._conn.select(
            f"SELECT * FROM blobs WHERE blob_id = {quote(blob_id(cell))}"
        )
        if not rows:
            log.error("blob %s is missing; its column reads as empty", blob_id(cell))
            return ""
        ordered = sorted(rows, key=lambda r: int(r["seq"]))
        return decode("".join(r["chunk"] for r in ordered))

    def _release(self, rows: list[dict[str, str]], columns) -> None:
        """Drop the chunks behind any spilled cell in these rows."""
        for row in rows:
            for column in columns:
                cell = row.get(column, "")
                if is_blob(cell):
                    self._conn.command(
                        f"DELETE FROM blobs WHERE blob_id = {quote(blob_id(cell))}"
                    )

    # -- projects ---------------------------------------------------------- #

    def create_project(
        self, project_id: str, name: str, root_dir: Path, tool_policy: list[str]
    ) -> ProjectRecord:
        record = ProjectRecord(
            id=project_id, name=name, root_dir=str(root_dir),
            tool_policy=tool_policy, created_at=utcnow(),
        )
        with self._lock:
            with self._txn():
                if self._select("projects", id=project_id):
                    raise StoreError(f"project {project_id!r} already exists")
                self._insert("projects", [
                    self._put(record.id), self._put(record.name),
                    self._put(record.root_dir), self._put(json.dumps(record.tool_policy)),
                    self._put(record.created_at.isoformat()),
                ])
        return record

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with self._lock:
            rows = self._select("projects", id=project_id)
            return self._project(rows[0]) if rows else None

    def list_projects(self) -> list[ProjectRecord]:
        with self._lock:
            rows = self._select("projects")
            projects = [self._project(r) for r in rows]
        return sorted(projects, key=lambda p: p.id)

    def delete_project(self, project_id: str) -> None:
        with self._lock:
            with self._txn():
                # No ON DELETE CASCADE here — foreign keys are RESTRICT-only,
                # and they key on the engine's Keystone id rather than on the
                # string ids this store relates rows by. So: children first.
                tasks = self._select("tasks", project_id=project_id)
                self._release(tasks, SPILLABLE["tasks"])
                agents = self._select("agents", project_id=project_id)
                self._release(agents, SPILLABLE["agents"])
                projects = self._select("projects", id=project_id)
                self._release(projects, SPILLABLE["projects"])
                for table in ("tasks", "agents"):
                    self._conn.command(
                        f"DELETE FROM {table}{self._where(project_id=project_id)}"
                    )
                self._conn.command(f"DELETE FROM projects{self._where(id=project_id)}")

    def _project(self, row: dict[str, str]) -> ProjectRecord:
        return ProjectRecord(
            id=self._get(row["id"]),
            name=self._get(row["name"]),
            root_dir=self._get(row["root_dir"]),
            tool_policy=json.loads(self._get(row["tool_policy"])),
            created_at=datetime.fromisoformat(self._get(row["created_at"])),
        )

    # -- agents ------------------------------------------------------------ #

    def add_agent(
        self, project_id: str, name: str, role: AgentRole, config: AgentConfig
    ) -> ProjectAgentRecord:
        record = ProjectAgentRecord(
            project_id=project_id, name=name, runtime_name=config.name,
            role=role, config=config, created_at=utcnow(),
        )
        with self._lock:
            with self._txn():
                # The three rules the SQLite schema states as constraints. Here
                # they are checks, and only the lock makes them atomic.
                if self._select("agents", project_id=project_id, name=name):
                    raise StoreError(
                        f"agent {name!r} already exists in project {project_id!r}"
                    )
                if self._select("agents", runtime_name=config.name):
                    raise StoreError(f"runtime agent name {config.name!r} is taken")
                if role == "manager":
                    existing = self._select("agents", project_id=project_id, role="manager")
                    if existing:
                        raise StoreError(
                            f"project {project_id!r} already has a projectmanager"
                        )
                self._insert("agents", [
                    self._put(project_id), self._put(name), self._put(config.name),
                    self._put(role), self._put(config.model_dump_json()),
                    self._put(record.created_at.isoformat()),
                ])
        return record

    def get_agent(self, project_id: str, name: str) -> ProjectAgentRecord | None:
        with self._lock:
            rows = self._select("agents", project_id=project_id, name=name)
            return self._agent(rows[0]) if rows else None

    def get_agent_by_runtime(self, runtime: str) -> ProjectAgentRecord | None:
        with self._lock:
            rows = self._select("agents", runtime_name=runtime)
            return self._agent(rows[0]) if rows else None

    def list_agents(self, project_id: str | None = None) -> list[ProjectAgentRecord]:
        with self._lock:
            rows = self._select("agents", project_id=project_id)
            agents = [self._agent(r) for r in rows]
        return sorted(agents, key=lambda a: (a.project_id, a.name))

    def delete_agent(self, project_id: str, name: str) -> None:
        with self._lock:
            with self._txn():
                rows = self._select("agents", project_id=project_id, name=name)
                self._release(rows, SPILLABLE["agents"])
                self._conn.command(
                    f"DELETE FROM agents{self._where(project_id=project_id, name=name)}"
                )

    def _agent(self, row: dict[str, str]) -> ProjectAgentRecord:
        return ProjectAgentRecord(
            project_id=self._get(row["project_id"]),
            name=self._get(row["name"]),
            runtime_name=self._get(row["runtime_name"]),
            role=self._get(row["role"]),
            config=AgentConfig.model_validate_json(self._get(row["config"])),
            created_at=datetime.fromisoformat(self._get(row["created_at"])),
        )

    # -- tasks ------------------------------------------------------------- #

    def create_task(
        self, task_id: str, project_id: str, agent: str, title: str, text: str,
        created_by: str, message_id: str,
    ) -> TaskRecord:
        record = TaskRecord(
            id=task_id, project_id=project_id, agent=agent, title=title, text=text,
            status="queued", created_by=created_by, message_id=message_id,
            created_at=utcnow(),
        )
        with self._lock:
            with self._txn():
                self._insert("tasks", [
                    self._put(task_id), self._put(project_id), self._put(agent),
                    self._put(title), self._put(text), self._put("queued"),
                    self._put(created_by), self._put(message_id),
                    NULL_CELL, NULL_CELL, NULL_CELL,
                    self._put(record.created_at.isoformat()), NULL_CELL, NULL_CELL,
                ])
        return record

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            rows = self._select("tasks", id=task_id)
            return self._task(rows[0]) if rows else None

    def list_tasks(
        self, project_id: str | None = None, status: str | None = None,
        agent: str | None = None, limit: int = 200,
    ) -> list[TaskRecord]:
        with self._lock:
            rows = self._select("tasks", project_id=project_id, status=status, agent=agent)
            tasks = [self._task(r) for r in rows]
        # There is no ORDER BY and no LIMIT in the grammar, so the sort and the
        # slice happen here. Same order the SQLite backend returns.
        tasks.sort(key=lambda t: (t.created_at, t.id), reverse=True)
        return tasks[:limit]

    def set_task_status(
        self, task_id: str, status: TaskStatus, *, result: str | None = None,
        error: str | None = None, cost_usd: float | None = None,
    ) -> None:
        now = utcnow().isoformat()
        with self._lock:
            with self._txn():
                sets = {"status": self._put(status)}
                if status == "running":
                    sets["started_at"] = self._put(now)
                elif status in ("done", "failed", "cancelled"):
                    sets["finished_at"] = self._put(now)
                replaced = [c for c, v in (("result", result), ("error", error))
                            if v is not None]
                if replaced:
                    # Overwriting a spilled cell must not orphan its chunks.
                    self._release(self._select("tasks", id=task_id), replaced)
                if result is not None:
                    sets["result"] = self._put(result)
                if error is not None:
                    sets["error"] = self._put(error[:4000])
                if cost_usd is not None:
                    sets["cost_usd"] = self._put(repr(cost_usd))
                assignments = ", ".join(f"{c} = {quote(v)}" for c, v in sets.items())
                self._conn.command(
                    f"UPDATE tasks SET {assignments}{self._where(id=task_id)}"
                )

    def cancel_if_queued(self, task_id: str) -> bool:
        """Cancel only from `queued`.

        One UPDATE whose WHERE names both the id and the expected status, so the
        rowcount *is* the compare-and-set result — no read-then-write window.
        """
        with self._lock:
            reply = self._conn.command(
                f"UPDATE tasks SET status = {quote(encode('cancelled'))},"
                f" finished_at = {quote(encode(utcnow().isoformat()))}"
                f"{self._where(id=task_id, status='queued')}"
            )
            return self._count(reply) > 0

    def cancel_queued_for_agent(self, agent: str, reason: str) -> int:
        with self._lock:
            reply = self._conn.command(
                f"UPDATE tasks SET status = {quote(encode('cancelled'))},"
                f" finished_at = {quote(encode(utcnow().isoformat()))},"
                f" error = {quote(encode(reason))}"
                f"{self._where(agent=agent, status='queued')}"
            )
            return self._count(reply)

    def fail_running(self, reason: str) -> int:
        with self._lock:
            reply = self._conn.command(
                f"UPDATE tasks SET status = {quote(encode('failed'))},"
                f" finished_at = {quote(encode(utcnow().isoformat()))},"
                f" error = {quote(encode(reason))}"
                f"{self._where(status='running')}"
            )
            return self._count(reply)

    def _task(self, row: dict[str, str]) -> TaskRecord:
        cost = self._get(row["cost_usd"])
        return TaskRecord(
            id=self._get(row["id"]),
            project_id=self._get(row["project_id"]),
            agent=self._get(row["agent"]),
            title=self._get(row["title"]),
            text=self._get(row["body"]),
            status=self._get(row["status"]),
            created_by=self._get(row["created_by"]),
            message_id=self._get(row["message_id"]),
            result=self._get(row["result"]),
            error=self._get(row["error"]),
            cost_usd=float(cost) if cost else None,
            created_at=datetime.fromisoformat(self._get(row["created_at"])),
            started_at=_opt_dt(self._get(row["started_at"])),
            finished_at=_opt_dt(self._get(row["finished_at"])),
        )


def _opt_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
