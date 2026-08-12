"""Where sessions and their transcripts live.

Two tables and nothing else. The pluggable project store this replaced carried
projects, agents, tasks, issues and milestones across two backends; a chat
console needs the list of conversations and what was said in them, so that is
all this is.

SQLite, one file, WAL. Single operator, one process — the concurrency story is
"there is only one writer", and WAL means a reader (the console polling) never
blocks it.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime
from pathlib import Path

from .models import Role, SessionConfig, Turn, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    name            TEXT PRIMARY KEY,
    cwd             TEXT NOT NULL,
    system_prompt   TEXT,
    allowed_tools   TEXT NOT NULL DEFAULT '',
    permission_mode TEXT NOT NULL DEFAULT 'bypassPermissions',
    model           TEXT,
    max_budget_usd  REAL NOT NULL DEFAULT 0.5,
    timeout_s       INTEGER NOT NULL DEFAULT 900,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session    TEXT NOT NULL,
    role       TEXT NOT NULL,
    text       TEXT NOT NULL,
    at         TEXT NOT NULL,
    message_id TEXT,
    failed     INTEGER NOT NULL DEFAULT 0
);

-- Every read of a transcript is "this session, newest last", and deleting a
-- session deletes its turns; both are this index.
CREATE INDEX IF NOT EXISTS turns_by_session ON turns(session, id);
"""


class SessionStoreError(Exception):
    """The store refused an operation. Carries the status the API should use."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _split_tools(text: str) -> list[str]:
    return [t for t in (text or "").split(",") if t]


class SessionStore:
    """Sessions and transcripts. Every method is safe to call from any thread."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False plus one lock: FastAPI runs sync route bodies
        # in a threadpool, so the connection outlives the thread that made it.
        self._db = sqlite3.connect(str(self.path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # -- sessions -----------------------------------------------------------

    def create(self, config: SessionConfig) -> SessionConfig:
        with self._lock:
            existing = self._db.execute(
                "SELECT 1 FROM sessions WHERE name = ?", (config.name,)
            ).fetchone()
            if existing:
                raise SessionStoreError(f"session {config.name!r} already exists", 409)
            self._db.execute(
                "INSERT INTO sessions (name, cwd, system_prompt, allowed_tools,"
                " permission_mode, model, max_budget_usd, timeout_s, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    config.name,
                    str(config.cwd),
                    config.system_prompt,
                    ",".join(config.allowed_tools),
                    config.permission_mode,
                    config.model,
                    config.max_budget_usd,
                    config.timeout_s,
                    config.created_at.isoformat(),
                ),
            )
            self._db.commit()
        return config

    def replace(self, config: SessionConfig) -> SessionConfig:
        with self._lock:
            cursor = self._db.execute(
                "UPDATE sessions SET cwd = ?, system_prompt = ?, allowed_tools = ?,"
                " permission_mode = ?, model = ?, max_budget_usd = ?, timeout_s = ?"
                " WHERE name = ?",
                (
                    str(config.cwd),
                    config.system_prompt,
                    ",".join(config.allowed_tools),
                    config.permission_mode,
                    config.model,
                    config.max_budget_usd,
                    config.timeout_s,
                    config.name,
                ),
            )
            if not cursor.rowcount:
                raise SessionStoreError(f"no session {config.name!r}", 404)
            self._db.commit()
        return config

    def delete(self, name: str) -> None:
        """Drop the session and everything it ever said."""
        with self._lock:
            cursor = self._db.execute("DELETE FROM sessions WHERE name = ?", (name,))
            if not cursor.rowcount:
                raise SessionStoreError(f"no session {name!r}", 404)
            self._db.execute("DELETE FROM turns WHERE session = ?", (name,))
            self._db.commit()

    def get(self, name: str) -> SessionConfig | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM sessions WHERE name = ?", (name,)
            ).fetchone()
        return self._to_config(row) if row else None

    def list(self) -> list[SessionConfig]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM sessions ORDER BY created_at, name"
            ).fetchall()
        return [self._to_config(row) for row in rows]

    @staticmethod
    def _to_config(row: sqlite3.Row) -> SessionConfig:
        return SessionConfig(
            name=row["name"],
            cwd=Path(row["cwd"]),
            system_prompt=row["system_prompt"],
            allowed_tools=_split_tools(row["allowed_tools"]),
            permission_mode=row["permission_mode"],
            model=row["model"],
            max_budget_usd=row["max_budget_usd"],
            timeout_s=row["timeout_s"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )

    # -- transcripts --------------------------------------------------------

    def add_turn(
        self,
        session: str,
        role: Role,
        text: str,
        message_id: str | None = None,
        failed: bool = False,
        at: datetime | None = None,
    ) -> Turn:
        when = at or utcnow()
        with self._lock:
            cursor = self._db.execute(
                "INSERT INTO turns (session, role, text, at, message_id, failed)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (session, role, text, when.isoformat(), message_id, int(failed)),
            )
            self._db.commit()
            turn_id = cursor.lastrowid
        return Turn(
            id=turn_id,
            session=session,
            role=role,
            text=text,
            at=when,
            message_id=message_id,
            failed=failed,
        )

    def history(self, session: str, limit: int = 200) -> list[Turn]:
        """The last `limit` turns, oldest first — reading order.

        The tail is what a chat window wants, but it wants it the right way
        round, so the newest-first query is reversed rather than the whole
        transcript being read and sliced.
        """
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM turns WHERE session = ? ORDER BY id DESC LIMIT ?",
                (session, limit),
            ).fetchall()
        return [
            Turn(
                id=row["id"],
                session=row["session"],
                role=row["role"],
                text=row["text"],
                at=datetime.fromisoformat(row["at"]),
                message_id=row["message_id"],
                failed=bool(row["failed"]),
            )
            for row in reversed(rows)
        ]

    def clear_history(self, session: str) -> int:
        """Forget the transcript, keeping the session. -> turns removed.

        The Claude Code session on the other side is untouched and still
        remembers: this clears what the console shows, not what the model knows.
        Starting genuinely fresh means deleting the session.
        """
        with self._lock:
            cursor = self._db.execute("DELETE FROM turns WHERE session = ?", (session,))
            self._db.commit()
            return cursor.rowcount

    def counts(self) -> dict[str, tuple[int, datetime | None]]:
        """session -> (turns, last turn's time), for the session list."""
        with self._lock:
            rows = self._db.execute(
                "SELECT session, COUNT(*) AS n, MAX(at) AS last FROM turns"
                " GROUP BY session"
            ).fetchall()
        return {
            row["session"]: (
                row["n"],
                datetime.fromisoformat(row["last"]) if row["last"] else None,
            )
            for row in rows
        }
