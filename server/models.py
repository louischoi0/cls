"""Pydantic schemas and status bookkeeping shared across the server."""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Accepted by `claude --permission-mode`; anything else aborts startup.
PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
)

# Tags the dispatcher gives its own meaning to; agents may not claim them.
RESERVED_TAGS = frozenset({"global"})
RESERVED_TAG_PREFIXES = ("session:", "agent:")

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

MessageStatus = Literal["queued", "running", "done", "failed"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AgentConfig(BaseModel):
    """One entry of agents.yaml."""

    model_config = ConfigDict(extra="forbid")

    name: str
    tags: list[str] = Field(default_factory=list)
    cwd: Path
    system_prompt: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    permission_mode: str = "bypassPermissions"
    # README asks for --max-turns, which the installed CLI (2.1.223) does not
    # have. --max-budget-usd is the equivalent cap it does support.
    max_budget_usd: float = Field(default=0.50, gt=0)
    timeout_s: int = Field(default=900, gt=0)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"agent name must match {_NAME_RE.pattern!r}, got {v!r}")
        return v

    @field_validator("tags")
    @classmethod
    def _check_tags(cls, v: list[str]) -> list[str]:
        for tag in v:
            if not tag.strip():
                raise ValueError("tags must not be blank")
            if tag in RESERVED_TAGS or tag.startswith(RESERVED_TAG_PREFIXES):
                raise ValueError(f"tag {tag!r} is reserved by the dispatcher")
        return v

    @field_validator("permission_mode")
    @classmethod
    def _check_permission_mode(cls, v: str) -> str:
        if v not in PERMISSION_MODES:
            raise ValueError(
                f"permission_mode {v!r} not one of {sorted(PERMISSION_MODES)}"
            )
        return v

    @field_validator("cwd")
    @classmethod
    def _expand_cwd(cls, v: Path) -> Path:
        return Path(v).expanduser()


class MessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)
    tags: list[str] = Field(min_length=1)
    topic: str | None = None

    @field_validator("tags")
    @classmethod
    def _strip_tags(cls, v: list[str]) -> list[str]:
        tags = [t.strip() for t in v if t and t.strip()]
        if not tags:
            raise ValueError("tags must contain at least one non-blank value")
        return tags


class MessageAccepted(BaseModel):
    message_id: str
    targets: list[str]
    topic: str


class TargetState(BaseModel):
    agent: str
    status: MessageStatus = "queued"
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None


class MessageRecord(BaseModel):
    message_id: str
    topic: str
    tags: list[str]
    targets: list[TargetState]
    created_at: datetime

    @property
    def status(self) -> MessageStatus:
        states = [t.status for t in self.targets]
        if all(s == "done" for s in states):
            return "done"
        if all(s in ("done", "failed") for s in states):
            return "failed"
        if any(s == "running" for s in states) or any(
            s in ("done", "failed") for s in states
        ):
            return "running"
        return "queued"

    def target(self, agent: str) -> TargetState:
        for t in self.targets:
            if t.agent == agent:
                return t
        raise KeyError(agent)


class MessageStatusResponse(BaseModel):
    message_id: str
    status: MessageStatus
    topic: str
    tags: list[str]
    targets: list[TargetState]
    created_at: datetime

    @classmethod
    def of(cls, record: MessageRecord) -> "MessageStatusResponse":
        return cls(
            message_id=record.message_id,
            status=record.status,
            topic=record.topic,
            tags=record.tags,
            targets=record.targets,
            created_at=record.created_at,
        )


class AgentInfo(BaseModel):
    name: str
    tags: list[str]
    cwd: str
    session_id: str | None
    queue_depth: int
    busy: bool


class StatusStore:
    """In-memory message status, bounded so a long uptime cannot grow it forever.

    README §6 accepts losing this on restart; logs and sessions are the durable
    record.
    """

    def __init__(self, capacity: int = 5000) -> None:
        self.capacity = capacity
        self._records: OrderedDict[str, MessageRecord] = OrderedDict()

    def create(self, message_id: str, topic: str, tags: list[str], targets: list[str]) -> MessageRecord:
        record = MessageRecord(
            message_id=message_id,
            topic=topic,
            tags=tags,
            targets=[TargetState(agent=a) for a in targets],
            created_at=utcnow(),
        )
        self._records[message_id] = record
        while len(self._records) > self.capacity:
            self._records.popitem(last=False)
        return record

    def get(self, message_id: str) -> MessageRecord | None:
        return self._records.get(message_id)

    def _update(self, message_id: str, agent: str, **fields) -> None:
        record = self._records.get(message_id)
        if record is None:
            return  # evicted, or a job outliving its record — not worth failing over
        try:
            target = record.target(agent)
        except KeyError:
            return
        for key, value in fields.items():
            setattr(target, key, value)

    def mark_running(self, message_id: str, agent: str) -> None:
        self._update(message_id, agent, status="running", started_at=utcnow())

    def mark_done(self, message_id: str, agent: str) -> None:
        self._update(message_id, agent, status="done", finished_at=utcnow())

    def mark_failed(self, message_id: str, agent: str, error: str) -> None:
        self._update(
            message_id,
            agent,
            status="failed",
            finished_at=utcnow(),
            error=error[:2000],
        )
