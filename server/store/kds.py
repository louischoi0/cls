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

import hashlib
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
from .codec import BLOB_PREFIX, NULL_CELL, MAX_CELL, blob_id, chunks, decode, encode, is_blob, quote
from .kwp import KwpConnection, KwpError

log = logging.getLogger("cc_automation.kds")

# Column 0 of every relation is the engine's Keystone id: an integer, assigned
# by the server, never supplied by INSERT. `rid` is that column; this store
# addresses rows by its own string ids and only ever reads it back.
# Column 0 of every relation is the engine's Keystone id: an integer, assigned
# by the server, never supplied by INSERT. `rid` is that column; this store
# addresses rows by its own string ids and only ever reads it back.
#
# The physical table name carries a fingerprint of its columns (`tasks_a1b2c3d4`).
# KDS has neither ALTER TABLE nor DROP TABLE, and re-declaring a table with an
# extra column answers EXISTS with the *old* schema — so a changed column list
# becomes a different table rather than silently corrupting the next INSERT.
# Superseded tables cannot be dropped, so they are left behind and reported at
# startup instead of hidden.
TABLES = {
    "projects": "(rid int64, id varchar, name varchar, root_dir varchar,"
                " tool_policy varchar, created_at varchar)",
    "agents": "(rid int64, project_id varchar, name varchar, runtime_name varchar,"
              " role varchar, config varchar, created_at varchar)",
    # `body` rather than `text`, so no column name meets the grammar.
    "tasks": "(rid int64, id varchar, project_id varchar, agent varchar,"
             " title varchar, body varchar, status varchar, created_by varchar,"
             " branch varchar, milestone_id varchar, message_id varchar,"
             " result varchar, error varchar,"
             " cost_usd varchar, created_at varchar, started_at varchar,"
             " finished_at varchar)",
    "blobs": "(rid int64, blob_id varchar, seq int64, chunk varchar)",
    "project_meta": "(rid int64, project_id varchar, mkey varchar, mvalue varchar)",
    "issues": "(rid int64, id varchar, project_id varchar, title varchar,"
              " body varchar, kind varchar, status varchar, created_by varchar,"
              " agent varchar, task_id varchar, branch varchar,"
              " milestone_id varchar, resolution varchar, created_at varchar,"
              " resolved_at varchar)",
    "milestones": "(rid int64, id varchar, project_id varchar, title varchar,"
                  " body varchar, target varchar, status varchar,"
                  " created_by varchar, branch varchar, source varchar,"
                  " position int64,"
                  " created_at varchar, completed_at varchar)",
}


def physical(logical: str) -> str:
    """`tasks` -> `tasks_<fingerprint of its column list>`."""
    return f"{logical}_{hashlib.sha256(TABLES[logical].encode()).hexdigest()[:8]}"


#: logical -> physical, resolved once so every query spells it the same way.
PHYSICAL = {name: physical(name) for name in TABLES}

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
    ("tasks", "branch"),
    ("issues", "branch"),
    ("blobs", "blob_id"),
    ("project_meta", "project_id"),
    ("issues", "id"),
    ("issues", "project_id"),
    ("issues", "status"),
    ("tasks", "milestone_id"),
    ("issues", "milestone_id"),
    ("milestones", "id"),
    ("milestones", "project_id"),
]

TASK_COLUMNS = [
    "id", "project_id", "agent", "title", "body", "status", "created_by",
    "branch", "message_id", "result", "error", "cost_usd", "created_at",
    "started_at", "finished_at",
]
PROJECT_COLUMNS = ["id", "name", "root_dir", "tool_policy", "created_at"]
AGENT_COLUMNS = ["project_id", "name", "runtime_name", "role", "config", "created_at"]

#: Columns that can hold a value long enough to be chunked into `blobs`.
SPILLABLE = {"tasks": ("title", "body", "result", "error"), "agents": ("config",),
             "projects": ("name", "root_dir", "tool_policy"),
             "project_meta": ("mvalue",),
             "issues": ("title", "body", "resolution"),
             "milestones": ("title", "body", "target")}

#: key under which the repository link lives in `project_meta`
GITHUB_KEY = "github_url"


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
            for logical, columns in TABLES.items():
                self._conn.command(
                    f"CREATE TABLE {PHYSICAL[logical]} {columns} BTREE"
                )
            for logical, column in CABINS:
                try:
                    self._conn.command(
                        f"CREATE CABIN ON {PHYSICAL[logical]}({column})"
                    )
                except KwpError as exc:
                    # Already declared is the normal case on every boot but the
                    # first; anything else costs speed, never correctness.
                    log.debug("cabin %s.%s: %s", logical, column, exc)
            self._report_superseded()
            self._conn.command("SYNC")

    def _report_superseded(self) -> None:
        """Name the tables an earlier schema left behind.

        Nothing can remove them — KDS has no DROP TABLE — so the honest thing is
        to say they are there and being ignored, rather than let a data file
        quietly accumulate relations nobody can account for.
        """
        try:
            listed = self._conn.command("SHOW TABLES").split()
        except KwpError:
            return
        live = set(PHYSICAL.values())
        stale = [
            name for name in listed
            if any(name.startswith(f"{logical}_") for logical in TABLES)
            and name not in live
        ]
        if stale:
            log.warning(
                "%d superseded table(s) in this data file, ignored: %s. KDS has "
                "no DROP TABLE, so reclaiming the space means a fresh data file.",
                len(stale), ", ".join(sorted(stale)),
            )

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
        return self._conn.select(f"SELECT * FROM {PHYSICAL[table]}{self._where(**eq)}")

    def _insert(self, table: str, cells: list[str | int]) -> None:
        values = ", ".join(str(c) if isinstance(c, int) else quote(c) for c in cells)
        self._conn.command(f"INSERT INTO {PHYSICAL[table]} VALUES ({values})")

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
            f"SELECT * FROM {PHYSICAL['blobs']} WHERE blob_id = {quote(blob_id(cell))}"
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
                        f"DELETE FROM {PHYSICAL['blobs']} WHERE blob_id = {quote(blob_id(cell))}"
                    )

    # -- projects ---------------------------------------------------------- #

    def create_project(
        self, project_id: str, name: str, root_dir: Path, tool_policy: list[str],
        github_url: str | None = None,
    ) -> ProjectRecord:
        record = ProjectRecord(
            id=project_id, name=name, root_dir=str(root_dir),
            tool_policy=tool_policy, created_at=utcnow(), github_url=github_url,
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
                if github_url:
                    self._set_meta(project_id, GITHUB_KEY, github_url)
        return record

    def get_project(self, project_id: str) -> ProjectRecord | None:
        with self._lock:
            rows = self._select("projects", id=project_id)
            if not rows:
                return None
            record = self._project(rows[0])
            record.github_url = self._get_meta(project_id, GITHUB_KEY)
            return record

    def list_projects(self) -> list[ProjectRecord]:
        with self._lock:
            projects = [self._project(r) for r in self._select("projects")]
            # One pass over the side table rather than a query per project.
            meta = {
                self._get(r["project_id"]): self._get(r["mvalue"])
                for r in self._select("project_meta", mkey=GITHUB_KEY)
            }
            for record in projects:
                record.github_url = meta.get(record.id)
        return sorted(projects, key=lambda p: p.id)

    def set_secret(self, project_id: str, key: str, value: str | None) -> None:
        with self._lock:
            with self._txn():
                self._set_meta(project_id, f"secret:{key}", value)

    def get_secret(self, project_id: str, key: str) -> str | None:
        with self._lock:
            return self._get_meta(project_id, f"secret:{key}")

    def set_project_github(self, project_id: str, github_url: str | None) -> None:
        with self._lock:
            with self._txn():
                self._set_meta(project_id, GITHUB_KEY, github_url)

    def set_project_tool_policy(self, project_id: str, tool_policy: list[str]) -> None:
        with self._lock:
            with self._txn():
                rows = self._select("projects", id=project_id)
                if not rows:
                    return
                # The old value may have spilled into `blobs`; replacing the
                # cell without releasing it would orphan those chunks.
                self._release(rows, ["tool_policy"])
                cell = self._put(json.dumps(tool_policy))
                self._conn.command(
                    f"UPDATE {PHYSICAL['projects']} SET tool_policy = {quote(cell)}"
                    f"{self._where(id=project_id)}"
                )

    def _set_meta(self, project_id: str, key: str, value: str | None) -> None:
        """Upsert, by hand: there is no ON CONFLICT and no UPSERT in the grammar."""
        rows = self._select("project_meta", project_id=project_id, mkey=key)
        self._release(rows, SPILLABLE["project_meta"])
        if rows:
            self._conn.command(
                f"DELETE FROM {PHYSICAL['project_meta']}"
                f"{self._where(project_id=project_id, mkey=key)}"
            )
        if value is not None:
            self._insert("project_meta", [
                self._put(project_id), self._put(key), self._put(value),
            ])

    def _get_meta(self, project_id: str, key: str) -> str | None:
        rows = self._select("project_meta", project_id=project_id, mkey=key)
        return self._get(rows[0]["mvalue"]) if rows else None

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
                meta = self._select("project_meta", project_id=project_id)
                self._release(meta, SPILLABLE["project_meta"])
                issues = self._select("issues", project_id=project_id)
                self._release(issues, SPILLABLE["issues"])
                milestones = self._select("milestones", project_id=project_id)
                self._release(milestones, SPILLABLE["milestones"])
                for table in ("tasks", "agents", "project_meta", "issues",
                              "milestones"):
                    self._conn.command(
                        f"DELETE FROM {PHYSICAL[table]}"
                        f"{self._where(project_id=project_id)}"
                    )
                self._conn.command(
                    f"DELETE FROM {PHYSICAL['projects']}{self._where(id=project_id)}"
                )

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

    def update_agent_config(
        self, project_id: str, name: str, config: AgentConfig
    ) -> None:
        with self._lock:
            with self._txn():
                rows = self._select("agents", project_id=project_id, name=name)
                if not rows:
                    return
                # A config can be long enough to spill; replacing the cell
                # without releasing the old one would orphan its chunks.
                self._release(rows, ["config"])
                cell = self._put(config.model_dump_json())
                self._conn.command(
                    f"UPDATE {PHYSICAL['agents']} SET config = {quote(cell)}"
                    f"{self._where(project_id=project_id, name=name)}"
                )

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
                    f"DELETE FROM {PHYSICAL['agents']}"
                    f"{self._where(project_id=project_id, name=name)}"
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
        self, task_id: str, project_id: str, agent: str | None, title: str,
        text: str, created_by: str, message_id: str | None,
        branch: str | None = None, milestone_id: str | None = None,
        status: TaskStatus = "queued",
    ) -> TaskRecord:
        if status != "backlog" and not agent:
            raise StoreError(f"a {status} task must name an agent")
        record = TaskRecord(
            id=task_id, project_id=project_id, agent=agent, title=title, text=text,
            status=status, created_by=created_by, message_id=message_id,
            branch=branch, milestone_id=milestone_id, created_at=utcnow(),
        )
        with self._lock:
            with self._txn():
                self._insert("tasks", [
                    self._put(task_id), self._put(project_id), self._put(agent),
                    self._put(title), self._put(text), self._put(status),
                    self._put(created_by), self._put(branch),
                    self._put(milestone_id), self._put(message_id),
                    NULL_CELL, NULL_CELL, NULL_CELL,
                    self._put(record.created_at.isoformat()), NULL_CELL, NULL_CELL,
                ])
        return record

    def assign_task(
        self, task_id: str, agent: str, message_id: str,
        branch: str | None = None, milestone_id: str | None = None,
    ) -> bool:
        """One UPDATE whose WHERE names the expected status, so the rowcount is
        the compare-and-set result — the same shape as `cancel_if_queued`."""
        sets = {
            "agent": encode(agent),
            "message_id": encode(message_id),
            "status": encode("queued"),
        }
        for column, value in (("branch", branch), ("milestone_id", milestone_id)):
            if value is not None:
                sets[column] = encode(value)
        assignments = ", ".join(f"{c} = {quote(v)}" for c, v in sets.items())
        with self._lock:
            reply = self._conn.command(
                f"UPDATE {PHYSICAL['tasks']} SET {assignments}"
                f"{self._where(id=task_id, status='backlog')}"
            )
            return self._count(reply) > 0

    def get_task(self, task_id: str) -> TaskRecord | None:
        with self._lock:
            rows = self._select("tasks", id=task_id)
            return self._task(rows[0]) if rows else None

    def list_tasks(
        self, project_id: str | None = None, status: str | None = None,
        agent: str | None = None, limit: int = 200, branch: str | None = None,
        milestone_id: str | None = None,
    ) -> list[TaskRecord]:
        with self._lock:
            rows = self._select("tasks", project_id=project_id, status=status,
                                agent=agent, branch=branch,
                                milestone_id=milestone_id)
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
                    f"UPDATE {PHYSICAL['tasks']} SET {assignments}{self._where(id=task_id)}"
                )

    def cancel_if_queued(self, task_id: str) -> bool:
        """Cancel work that has not started: `queued` or `backlog`.

        Each UPDATE names both the id and the expected status, so the rowcount
        *is* the compare-and-set result — no read-then-write window. There is no
        `IN` in the grammar, and a row holds one status, so trying each in turn
        is the same thing written twice.
        """
        with self._lock:
            for status in ("queued", "backlog"):
                reply = self._conn.command(
                    f"UPDATE {PHYSICAL['tasks']} SET"
                    f" status = {quote(encode('cancelled'))},"
                    f" finished_at = {quote(encode(utcnow().isoformat()))}"
                    f"{self._where(id=task_id, status=status)}"
                )
                if self._count(reply) > 0:
                    return True
            return False

    def cancel_queued_for_agent(self, agent: str, reason: str) -> int:
        with self._lock:
            reply = self._conn.command(
                f"UPDATE {PHYSICAL['tasks']} SET status = {quote(encode('cancelled'))},"
                f" finished_at = {quote(encode(utcnow().isoformat()))},"
                f" error = {quote(encode(reason))}"
                f"{self._where(agent=agent, status='queued')}"
            )
            return self._count(reply)

    def fail_running(self, reason: str) -> int:
        with self._lock:
            reply = self._conn.command(
                f"UPDATE {PHYSICAL['tasks']} SET status = {quote(encode('failed'))},"
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
            branch=self._get(row["branch"]),
            milestone_id=self._get(row["milestone_id"]),
            message_id=self._get(row["message_id"]),
            result=self._get(row["result"]),
            error=self._get(row["error"]),
            cost_usd=float(cost) if cost else None,
            created_at=datetime.fromisoformat(self._get(row["created_at"])),
            started_at=_opt_dt(self._get(row["started_at"])),
            finished_at=_opt_dt(self._get(row["finished_at"])),
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
            with self._txn():
                self._insert("issues", [
                    self._put(issue_id), self._put(project_id), self._put(title),
                    self._put(body), self._put(kind), self._put("open"),
                    self._put(created_by), self._put(agent), self._put(task_id),
                    self._put(branch), self._put(milestone_id), NULL_CELL,
                    self._put(record.created_at.isoformat()), NULL_CELL,
                ])
        return record

    def get_issue(self, issue_id: str) -> IssueRecord | None:
        with self._lock:
            rows = self._select("issues", id=issue_id)
            return self._issue(rows[0]) if rows else None

    def list_issues(
        self, project_id: str | None = None, status: str | None = None,
        kind: str | None = None, limit: int = 200,
        agent: str | None = None, branch: str | None = None,
        milestone_id: str | None = None,
    ) -> list[IssueRecord]:
        with self._lock:
            rows = self._select("issues", project_id=project_id, status=status,
                                kind=kind, agent=agent, branch=branch,
                                milestone_id=milestone_id)
            issues = [self._issue(r) for r in rows]
        issues.sort(key=lambda i: (i.created_at, i.id), reverse=True)
        return issues[:limit]

    def resolve_issue(
        self, issue_id: str, status: IssueStatus, resolution: str
    ) -> bool:
        """One UPDATE naming the expected status, so the rowcount is the answer."""
        with self._lock:
            with self._txn():
                # The resolution may be long enough to spill, so it is written
                # before the row is touched.
                cell = self._put(resolution)
                reply = self._conn.command(
                    f"UPDATE {PHYSICAL['issues']} SET status = {quote(encode(status))},"
                    f" resolution = {quote(cell)},"
                    f" resolved_at = {quote(encode(utcnow().isoformat()))}"
                    f"{self._where(id=issue_id, status='open')}"
                )
                moved = self._count(reply) > 0
                if not moved and is_blob(cell):
                    # Nothing took the value; do not leave its chunks behind.
                    self._conn.command(
                        f"DELETE FROM {PHYSICAL['blobs']} WHERE blob_id = {quote(blob_id(cell))}"
                    )
                return moved

    def _issue(self, row: dict[str, str]) -> IssueRecord:
        return IssueRecord(
            id=self._get(row["id"]),
            project_id=self._get(row["project_id"]),
            title=self._get(row["title"]),
            body=self._get(row["body"]),
            kind=self._get(row["kind"]),
            status=self._get(row["status"]),
            created_by=self._get(row["created_by"]),
            agent=self._get(row["agent"]),
            task_id=self._get(row["task_id"]),
            branch=self._get(row["branch"]),
            milestone_id=self._get(row["milestone_id"]),
            resolution=self._get(row["resolution"]),
            created_at=datetime.fromisoformat(self._get(row["created_at"])),
            resolved_at=_opt_dt(self._get(row["resolved_at"])),
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
            with self._txn():
                self._insert("milestones", [
                    self._put(milestone_id), self._put(project_id), self._put(title),
                    self._put(body), self._put(target), self._put("planned"),
                    self._put("user"), self._put(branch), self._put(source),
                    position,
                    self._put(record.created_at.isoformat()), NULL_CELL,
                ])
        return record

    def get_milestone(self, milestone_id: str) -> MilestoneRecord | None:
        with self._lock:
            rows = self._select("milestones", id=milestone_id)
            return self._milestone(rows[0]) if rows else None

    def list_milestones(
        self, project_id: str | None = None, status: str | None = None
    ) -> list[MilestoneRecord]:
        with self._lock:
            rows = self._select("milestones", project_id=project_id, status=status)
            milestones = [self._milestone(r) for r in rows]
        milestones.sort(key=lambda m: (m.position, m.created_at))
        return milestones

    def update_milestone(self, milestone_id: str, **fields) -> bool:
        allowed = {"title", "body", "target", "branch", "status", "position"}
        sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
        with self._lock:
            rows = self._select("milestones", id=milestone_id)
            if not rows:
                return False
            if not sets:
                return True
            with self._txn():
                spilled = [c for c in SPILLABLE["milestones"] if c in sets]
                if spilled:
                    self._release(rows, spilled)
                cells = {
                    k: (str(v) if k == "position" else quote(self._put(str(v))))
                    for k, v in sets.items()
                }
                if "status" in sets:
                    cells["completed_at"] = quote(
                        self._put(utcnow().isoformat())
                        if sets["status"] in ("done", "abandoned") else None
                    )
                assignments = ", ".join(f"{k} = {v}" for k, v in cells.items())
                self._conn.command(
                    f"UPDATE {PHYSICAL['milestones']} SET {assignments}"
                    f"{self._where(id=milestone_id)}"
                )
            return True

    def delete_milestone(self, milestone_id: str) -> None:
        with self._lock:
            with self._txn():
                # Tasks and issues outlive their milestone rather than vanishing
                # with it: the work happened, whatever became of the goal.
                for table in ("tasks", "issues"):
                    if self._select(table, milestone_id=milestone_id):
                        self._conn.command(
                            f"UPDATE {PHYSICAL[table]}"
                            f" SET milestone_id = {quote(NULL_CELL)}"
                            f"{self._where(milestone_id=milestone_id)}"
                        )
                rows = self._select("milestones", id=milestone_id)
                self._release(rows, SPILLABLE["milestones"])
                self._conn.command(
                    f"DELETE FROM {PHYSICAL['milestones']}"
                    f"{self._where(id=milestone_id)}"
                )

    def _milestone(self, row: dict[str, str]) -> MilestoneRecord:
        return MilestoneRecord(
            id=self._get(row["id"]),
            project_id=self._get(row["project_id"]),
            title=self._get(row["title"]),
            body=self._get(row["body"]),
            target=self._get(row["target"]),
            status=self._get(row["status"]),
            created_by=self._get(row["created_by"]),
            branch=self._get(row["branch"]),
            source=self._get(row["source"]),
            position=int(row["position"]),
            created_at=datetime.fromisoformat(self._get(row["created_at"])),
            completed_at=_opt_dt(self._get(row["completed_at"])),
        )


def _opt_dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
