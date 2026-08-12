"""What the API accepts and returns.

The vocabulary is small on purpose: a **session** is one Claude Code
conversation the operator created, and a **message** is one turn in it. There is
no project, task, issue or milestone here any more — this server is a chat
console with several sessions, not a work-management system.
"""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: `claude --permission-mode`. Anything that can prompt hangs a worker until
#: `timeout_s`, because `claude -p` has nobody to ask — so the safe ones are the
#: only ones offered.
PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
)

#: A session name is also a URL path segment and a log topic, so it is kept to
#: what is safe in both.
NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

MessageStatus = Literal["queued", "running", "done", "failed"]

#: Who said one turn. `system` is the server itself — a session reset, a run
#: that died — and never the model.
Role = Literal["user", "agent", "system"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SessionCreate(BaseModel):
    """`POST /sessions`: one session, as the operator asks for it.

    The definition used to come from `agents.yaml`, loaded once at startup and
    fatal if wrong. Sessions are made and unmade at runtime now, so this is a
    request body — and every rule about what a session may be lives here, where
    a bad one comes back as a 422 instead of a stack trace.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    cwd: Path
    system_prompt: str | None = None
    #: The real containment boundary — `permission_mode` never prompts, so a
    #: session can use exactly what is listed here and nothing else.
    allowed_tools: list[str] = Field(default_factory=list)
    permission_mode: str = "bypassPermissions"
    #: `claude --model`; None leaves the CLI on its own default
    model: str | None = None
    #: Per-turn spend cap. The installed CLI has no `--max-turns`, so this and
    #: `timeout_s` are what bound one run.
    max_budget_usd: float = Field(default=0.50, gt=0)
    timeout_s: int = Field(default=900, gt=0)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not NAME_RE.match(v):
            raise ValueError(f"session name must match {NAME_RE.pattern!r}, got {v!r}")
        return v

    @field_validator("permission_mode")
    @classmethod
    def _check_permission_mode(cls, v: str) -> str:
        if v not in PERMISSION_MODES:
            raise ValueError(
                f"permission_mode {v!r} not one of {sorted(PERMISSION_MODES)}"
            )
        return v

    def to_config(self) -> "SessionConfig":
        return SessionConfig(**self.model_dump())


class SessionConfig(SessionCreate):
    """A session that exists: what was asked for, plus when it was made.

    Subclassing the request body is what keeps one set of rules — a name the API
    would refuse cannot reach the store through some other door.
    """

    created_at: datetime = Field(default_factory=utcnow)


class SessionUpdate(BaseModel):
    """`PATCH /sessions/{name}`. Every field optional; absent means unchanged."""

    model_config = ConfigDict(extra="forbid")

    cwd: Path | None = None
    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    permission_mode: str | None = None
    model: str | None = None
    max_budget_usd: float | None = Field(default=None, gt=0)
    timeout_s: int | None = Field(default=None, gt=0)


class SessionInfo(BaseModel):
    """One session as the console shows it: its definition plus what it is doing."""

    name: str
    cwd: str
    system_prompt: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    permission_mode: str = "bypassPermissions"
    model: str | None = None
    max_budget_usd: float = 0.50
    timeout_s: int = 900
    created_at: datetime
    #: The Claude Code session id, minted on the first run and reused via
    #: `--resume`. None until then, which is what "never spoken to" looks like.
    session_id: str | None = None
    queue_depth: int = 0
    busy: bool = False
    turns: int = 0
    last_at: datetime | None = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)


class ChatAccepted(BaseModel):
    message_id: str
    session: str


class Turn(BaseModel):
    """One line of a transcript, as the console replays it."""

    id: int | None = None
    session: str
    role: Role
    text: str
    at: datetime
    #: ties a user turn to the reply that answered it, and both to the live feed
    message_id: str | None = None
    failed: bool = False


class MessageRecord(BaseModel):
    """One turn in flight, for `GET /messages/{id}`.

    A message goes to exactly one session — there is no fan-out and no tag
    routing any more — so this is a single status rather than a list of targets.
    """

    message_id: str
    session: str
    text: str
    status: MessageStatus = "queued"
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class StatusStore:
    """In-memory message status, bounded so a long uptime cannot grow it forever.

    Losing this on a restart is acceptable: the transcript in the store and the
    Markdown log are the durable record.
    """

    def __init__(self, capacity: int = 5000) -> None:
        self.capacity = capacity
        self._records: OrderedDict[str, MessageRecord] = OrderedDict()

    def create(self, message_id: str, session: str, text: str) -> MessageRecord:
        record = MessageRecord(message_id=message_id, session=session, text=text)
        self._records[message_id] = record
        self._records.move_to_end(message_id)
        while len(self._records) > self.capacity:
            self._records.popitem(last=False)
        return record

    def get(self, message_id: str) -> MessageRecord | None:
        return self._records.get(message_id)

    # The three the worker calls. `agent` is accepted and ignored so the runner
    # keeps one signature whether or not a message can fan out.
    def mark_running(self, message_id: str, agent: str | None = None) -> None:
        record = self._records.get(message_id)
        if record is not None:
            record.status = "running"
            record.started_at = utcnow()

    def mark_done(self, message_id: str, agent: str | None = None) -> None:
        record = self._records.get(message_id)
        if record is not None:
            record.status = "done"
            record.finished_at = utcnow()

    def mark_failed(
        self, message_id: str, agent: str | None = None, error: str | None = None
    ) -> None:
        record = self._records.get(message_id)
        if record is not None:
            record.status = "failed"
            record.finished_at = utcnow()
            record.error = error
