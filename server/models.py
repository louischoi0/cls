"""Pydantic schemas and status bookkeeping shared across the server."""

from __future__ import annotations

import re
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

# Accepted by `claude --permission-mode`; anything else aborts startup.
PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"}
)

# Tags the dispatcher gives its own meaning to; agents may not claim them.
RESERVED_TAGS = frozenset({"global"})
RESERVED_TAG_PREFIXES = ("session:", "agent:", "project:")

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

MessageStatus = Literal["queued", "running", "done", "failed"]
#: `backlog` is work that exists but has not been given to anyone: an imported
#: document's task list lands here. It has no agent and no queue entry, so it
#: costs nothing until someone assigns it. Every other status implies an agent.
TaskStatus = Literal["backlog", "queued", "running", "done", "failed", "cancelled"]
#: An issue is what stops a project moving: a decision only a human can make,
#: or a run that broke unexpectedly. Tasks are work; issues are what blocks it.
IssueStatus = Literal["open", "resolved", "dismissed"]
IssueKind = Literal["decision", "crash", "blocker"]
#: A milestone is the unit of intent: a goal the project is working toward.
#: Tasks are the work it takes, and issues are what stops that work.
#:   milestone  →  tasks  →  issues
#:
#: **Only a person creates a milestone.** Agents — the projectmanager included —
#: create tasks, raise issues and write logs, but the goals themselves are the
#: operator's; a manager that could mint its own milestones could invent its own
#: mandate. `created_by` is therefore always "user" and the plan vocabulary has
#: no milestone verb at all.
MilestoneStatus = Literal["planned", "active", "done", "abandoned"]

#: What an agent is doing right now.
#:
#: `idle`, `queued` and `planning` are facts the server holds. The rest are
#: **inferred from the words of the task in flight** — the agent does not report
#: what it is doing, so `reviewing` means "its task says review", not that it is
#: certainly reviewing. `working` is the honest fallback when nothing matches.
AgentActivity_ = Literal[
    "idle", "queued", "planning", "coding", "reviewing", "testing",
    "researching", "writing", "working",
]

#: title keyword -> inferred activity, first match wins
ACTIVITY_HINTS = (
    (("review", "audit", "critique", "inspect"), "reviewing"),
    (("test", "pytest", "spec", "coverage"), "testing"),
    (("research", "investigate", "explore", "compare", "find out"), "researching"),
    (("document", "write up", "readme", "docs", "changelog"), "writing"),
    (("implement", "add", "build", "fix", "refactor", "migrate", "code"), "coding"),
)


def infer_activity(title: str, text: str = "") -> str:
    """Guess what a task is, from what it says it is."""
    haystack = f"{title} {text}".lower()
    for words, activity in ACTIVITY_HINTS:
        if any(word in haystack for word in words):
            return activity
    return "working"
AgentRole = Literal["manager", "worker"]

#: Separates a project id from an agent name in the name the registry sees.
RUNTIME_SEP = "__"
#: Tools a project agent may be granted when the project does not say otherwise.
DEFAULT_TOOL_POLICY = ("Read", "Glob", "Grep", "Edit", "Write")
#: Everything an agent can be given. `Bash` is the line between "edits files in
#: its directory" and "runs commands on this machine"; a project whose policy
#: includes it has agents that can do anything the server account can, without
#: being asked. Offered as one switch because half-granting it is a fiction —
#: an agent with Bash has Edit and Write whatever the list says.
FULL_TOOL_POLICY = (
    "Read", "Glob", "Grep", "Edit", "Write", "Bash",
    "WebFetch", "WebSearch", "NotebookEdit", "TodoWrite",
)

#: Models an agent may run on. v1 is Claude-only — every agent is a `claude -p`
#: subprocess, so a GPT or Codex entry here would need a different runner, not
#: a different flag. The shape is a mapping so adding a provider later is a new
#: block rather than a rewrite.
MODELS = {
    "opus": "Opus — most capable, slowest, dearest",
    "sonnet": "Sonnet — the working default",
    "haiku": "Haiku — fastest and cheapest, for narrow work",
    "fable": "Fable — latest frontier model",
}
MAX_AGENTS_PER_PROJECT = 12
MAX_PLAN_ACTIONS = 25


def runtime_name(project_id: str, agent_name: str) -> str:
    return f"{project_id}{RUNTIME_SEP}{agent_name}"


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
    #: `claude --model`; None leaves the CLI on its own default
    model: str | None = None
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
    #: set for agents owned by a project; None for agents.yaml agents
    project: str | None = None
    role: AgentRole | None = None
    activity: AgentActivity_ = "idle"
    working_on: str | None = None
    model: str | None = None
    #: what this session is *for* — the console lists sessions by their work
    #: rather than by agent name. See `SessionSubject`.
    subject: str | None = None
    subject_kind: Literal["task", "issue"] | None = None
    subject_id: str | None = None
    #: the goal that work serves, when it has one
    milestone: str | None = None
    #: it asked the operator something and nobody has answered
    waiting: bool = False


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


# --------------------------------------------------------------------------- #
# Projects (docs/PROJECTS.md)
# --------------------------------------------------------------------------- #


class ProjectAgentCreate(BaseModel):
    """An agent to create inside a project. `cwd` is relative to root_dir."""

    model_config = ConfigDict(extra="forbid")

    name: str
    role: AgentRole = "worker"
    system_prompt: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    permission_mode: str = "bypassPermissions"
    cwd: str | None = None
    #: `claude --model`; None leaves the CLI on its own default
    model: str | None = None
    max_budget_usd: float = Field(default=0.50, gt=0)
    timeout_s: int = Field(default=900, gt=0)

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"agent name must match {_NAME_RE.pattern!r}, got {v!r}")
        if RUNTIME_SEP in v:
            raise ValueError(f"agent name may not contain {RUNTIME_SEP!r}")
        return v


class ProjectAgentUpdate(BaseModel):
    """What can change about an agent without recreating it.

    Absent means "leave it alone". `system_prompt` and `model` take `""` to
    clear, since None already means untouched — the same convention the project's
    `github_url` uses.

    Not here on purpose: `name` and `role`, because the runtime name is the key
    its session is filed under and its role is a database constraint; and `cwd`,
    because every agent in a project works in the project's directory.
    """

    model_config = ConfigDict(extra="forbid")

    system_prompt: str | None = None
    allowed_tools: list[str] | None = None
    permission_mode: str | None = None
    model: str | None = None
    max_budget_usd: float | None = Field(default=None, gt=0)
    timeout_s: int | None = Field(default=None, gt=0)


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    root_dir: Path
    tool_policy: list[str] | None = None
    manager: ProjectAgentCreate | None = None
    #: worker agents to create alongside the project, in order
    agents: list[ProjectAgentCreate] = Field(default_factory=list)
    #: github.com/owner/repo. Stored canonical; used to seed overview.md.
    github_url: str | None = None
    #: scan for design documents and turn each into a milestone at creation
    import_milestones: bool = False
    #: files and/or directories to scan; empty means `docs/`
    import_paths: list[str] = Field(default_factory=list)

    @field_validator("root_dir")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return Path(v).expanduser()


class ProjectRecord(BaseModel):
    id: str
    name: str
    root_dir: str
    tool_policy: list[str]
    created_at: datetime
    github_url: str | None = None


class ProjectInfo(ProjectRecord):
    #: what a creation with `import_milestones` actually did, reported once
    imported: "MilestoneImportResult | None" = None
    agents: list[str] = Field(default_factory=list)
    manager: str | None = None
    open_tasks: int = 0
    #: imported or drafted work waiting for an agent
    backlog_tasks: int = 0
    open_issues: int = 0
    milestones: int = 0
    milestones_done: int = 0


class ProjectAgentRecord(BaseModel):
    project_id: str
    name: str
    runtime_name: str
    role: AgentRole
    config: AgentConfig
    created_at: datetime


class ProjectUpdate(BaseModel):
    """Fields of a project that can change after it is created."""

    model_config = ConfigDict(extra="forbid")

    #: "" clears the link; None leaves it alone
    github_url: str | None = None
    #: The ceiling on what this project's agents may be granted. Widening it
    #: grants nothing by itself — an agent's tools were fixed when it was made.
    tool_policy: list[str] | None = None


class OverviewSyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: Replacing a brief the operator wrote is never implicit.
    overwrite: bool = False


class OverviewSyncResult(BaseModel):
    project_id: str
    written: bool
    source: str | None = None
    bytes: int = 0
    detail: str


class AgentState(BaseModel):
    """Everything about one agent at this moment.

    `AgentInfo` says an agent is busy; this says what it is busy *with*, what is
    waiting behind it, and what it has cost so far — the questions you actually
    have when a queue is not moving.
    """

    name: str
    project: str | None = None
    role: AgentRole | None = None
    cwd: str
    tags: list[str] = Field(default_factory=list)
    model: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    permission_mode: str = "bypassPermissions"
    system_prompt: str | None = None
    max_budget_usd: float = 0.0
    timeout_s: int = 0

    #: live worker state
    busy: bool = False
    #: idle / queued / planning, or what the task in flight looks like
    activity: AgentActivity_ = "idle"
    #: the task it is on right now, when there is one
    working_on: str | None = None
    #: the run in flight, for watching its output live. Set for anything the
    #: agent is doing — a task, a planning round, a plain message — because all
    #: three are one job with one message id.
    running_message_id: str | None = None
    #: what this session is for: the work in flight, or the last it did. The
    #: console lists sessions by their subject, because "what is this working
    #: on" is the question, and the agent's name is only how it is addressed.
    subject: str | None = None
    subject_kind: Literal["task", "issue"] | None = None
    subject_id: str | None = None
    milestone: str | None = None
    waiting: bool = False
    activity_detail: str = ""
    activity_since: datetime | None = None
    queue_depth: int = 0
    session_id: str | None = None
    #: the task in flight right now, if this agent is running one
    running: "TaskRecord | None" = None
    queued: list["TaskRecord"] = Field(default_factory=list)
    recent: list["TaskRecord"] = Field(default_factory=list)
    open_issues: list["IssueRecord"] = Field(default_factory=list)

    tasks_done: int = 0
    tasks_failed: int = 0
    cost_usd: float = 0.0
    last_active: datetime | None = None


class ProjectSettings(BaseModel):
    """What an operator can configure per project, credentials excluded.

    The token is **never** in a response — only whether one is set and its last
    four characters, which is enough to tell two tokens apart and useless to
    anyone who reads it.
    """

    project_id: str
    github_url: str | None = None
    git_user_name: str = ""
    git_user_email: str = ""
    #: agents may run push/merge, not just read
    allow_push: bool = False
    github_token_set: bool = False
    github_token_hint: str = ""


class ProjectSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_url: str | None = None
    git_user_name: str | None = None
    git_user_email: str | None = None
    allow_push: bool | None = None
    #: write-only. "" clears it; omitted leaves the stored one alone.
    github_token: str | None = None


class MilestoneCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    body: str = ""
    #: free text; the server never parses or enforces it
    target: str = ""
    branch: str | None = None


class MilestoneUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = None
    body: str | None = None
    target: str | None = None
    branch: str | None = None
    status: MilestoneStatus | None = None


class MilestoneRecord(BaseModel):
    id: str
    project_id: str
    title: str
    body: str
    target: str
    status: MilestoneStatus
    created_by: str
    branch: str | None = None
    #: the file this was imported from, relative to the project root
    source: str | None = None
    #: hand-ordered position, so a roadmap reads top to bottom
    position: int = 0
    created_at: datetime
    completed_at: datetime | None = None


class MilestoneImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: files and/or directories, relative to the project root. A directory is
    #: scanned for `*.md`; a file is taken as given. Empty means `docs/`.
    paths: list[str] = Field(default_factory=list)
    #: report what would be imported without writing anything
    dry_run: bool = False
    #: pull the document's work lists in as backlog tasks under its milestone
    tasks: bool = True
    #: pull its open questions, risks and decisions in as open issues
    issues: bool = True


class MilestoneImported(BaseModel):
    source: str
    title: str
    #: set unless this was a dry run or a skip
    milestone_id: str | None = None
    skipped: str | None = None
    #: how much of the document came in under this milestone
    tasks: int = 0
    issues: int = 0


class MilestoneImportResult(BaseModel):
    project_id: str
    scanned: list[str] = Field(default_factory=list)
    dry_run: bool = False
    imported: list[MilestoneImported] = Field(default_factory=list)
    skipped: list[MilestoneImported] = Field(default_factory=list)
    #: ids of everything created, flat, across all documents
    tasks_created: list[str] = Field(default_factory=list)
    issues_created: list[str] = Field(default_factory=list)
    detail: str = ""


class MilestoneProgress(BaseModel):
    """A milestone with the state of everything hanging off it."""

    milestone: MilestoneRecord
    tasks_total: int = 0
    tasks_done: int = 0
    tasks_failed: int = 0
    tasks_open: int = 0
    tasks_backlog: int = 0
    issues_open: int = 0
    cost_usd: float = 0.0

    @property
    def percent(self) -> int:
        return round(100 * self.tasks_done / self.tasks_total) if self.tasks_total else 0


class AgentActivity(BaseModel):
    agent: str
    role: AgentRole
    tasks_done: int = 0
    tasks_failed: int = 0
    tasks_open: int = 0
    cost_usd: float = 0.0
    busy: bool = False
    queue_depth: int = 0


class ProjectInsight(BaseModel):
    """Everything the project page needs to say how a project is actually going."""

    project_id: str
    name: str
    milestones: list[MilestoneProgress] = Field(default_factory=list)
    #: work not claimed by any milestone
    unassigned: MilestoneProgress | None = None
    tasks_by_status: dict[str, int] = Field(default_factory=dict)
    issues_by_status: dict[str, int] = Field(default_factory=dict)
    open_issues_by_kind: dict[str, int] = Field(default_factory=dict)
    agents: list[AgentActivity] = Field(default_factory=list)
    branches: list[str] = Field(default_factory=list)
    cost_usd: float = 0.0
    #: the newest handful of tasks and issues, already interleaved
    recent: list["SearchHit"] = Field(default_factory=list)


class SearchHit(BaseModel):
    """One result from the integrated search, whatever kind of thing it is."""

    type: Literal["task", "issue", "log", "milestone"]
    id: str
    project_id: str
    title: str
    snippet: str = ""
    status: str | None = None
    kind: str | None = None
    agent: str | None = None
    branch: str | None = None
    created_at: datetime
    #: console route that shows this hit in context
    href: str = ""


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    #: None files the task in the backlog: written down, given to nobody
    agent: str | None = None
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    #: git branch the work belongs to; free text, never checked out by the server
    branch: str | None = None
    #: the milestone this work serves
    milestone_id: str | None = None


class TaskAssign(BaseModel):
    """Hand a backlog task to an agent, which is what queues it."""

    model_config = ConfigDict(extra="forbid")

    agent: str
    branch: str | None = None
    milestone_id: str | None = None


class TaskRecord(BaseModel):
    id: str
    project_id: str
    #: None while the task sits in the backlog
    agent: str | None = None
    title: str
    text: str
    status: TaskStatus
    created_by: str
    branch: str | None = None
    milestone_id: str | None = None
    message_id: str | None = None
    result: str | None = None
    error: str | None = None
    cost_usd: float | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class IssueCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    body: str = ""
    kind: IssueKind = "blocker"
    #: short agent name, when the issue came out of one agent's work
    agent: str | None = None
    task_id: str | None = None
    branch: str | None = None
    #: set directly, or inherited from the task this issue came out of
    milestone_id: str | None = None


class IssueResolve(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resolution: str = ""
    #: dismissed means "not going to act on this", as distinct from resolved
    dismiss: bool = False


class IssueRecord(BaseModel):
    id: str
    project_id: str
    title: str
    body: str
    kind: IssueKind
    status: IssueStatus
    created_by: str
    agent: str | None = None
    task_id: str | None = None
    branch: str | None = None
    milestone_id: str | None = None
    resolution: str | None = None
    created_at: datetime
    resolved_at: datetime | None = None


# --- plan protocol (docs/PROJECTS.md FR-P5) -------------------------------- #


class CreateAgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["create_agent"]
    name: str
    system_prompt: str | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    cwd: str | None = None
    max_budget_usd: float | None = Field(default=None, gt=0)
    timeout_s: int | None = Field(default=None, gt=0)


class DeleteAgentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["delete_agent"]
    name: str


class CreateTaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["create_task"]
    agent: str
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    branch: str | None = None
    milestone_id: str | None = None


class AssignTaskAction(BaseModel):
    """Take a task out of the backlog and give it to a worker.

    The backlog is where imported documents land. The manager cannot invent a
    goal, but it can decide who does the work already written down under one.
    """

    model_config = ConfigDict(extra="forbid")

    op: Literal["assign_task"]
    task_id: str
    agent: str
    branch: str | None = None
    milestone_id: str | None = None


class CancelTaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["cancel_task"]
    task_id: str


class NoteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["note"]
    text: str


class RaiseIssueAction(BaseModel):
    """The manager says the project is blocked and needs a person."""

    model_config = ConfigDict(extra="forbid")

    op: Literal["raise_issue"]
    title: str = Field(min_length=1)
    body: str = ""
    kind: IssueKind = "blocker"
    agent: str | None = None
    branch: str | None = None


class ResolveIssueAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    op: Literal["resolve_issue"]
    issue_id: str
    resolution: str = ""


PlanAction = Annotated[
    CreateAgentAction
    | DeleteAgentAction
    | CreateTaskAction
    | AssignTaskAction
    | CancelTaskAction
    | RaiseIssueAction
    | ResolveIssueAction
    | NoteAction,
    Field(discriminator="op"),
]


class Plan(BaseModel):
    """What the manager is required to answer with.

    `actions` stays raw here on purpose: each one is validated separately, so a
    single malformed action is rejected on its own instead of discarding a plan
    that was otherwise usable.
    """

    model_config = ConfigDict(extra="ignore")

    summary: str | None = None
    actions: list[dict] = Field(default_factory=list)


PLAN_ACTION_ADAPTER: TypeAdapter[PlanAction] = TypeAdapter(PlanAction)


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    note: str | None = None


class RejectedAction(BaseModel):
    action: dict
    reason: str


class PlanResult(BaseModel):
    project_id: str
    message_id: str
    summary: str | None = None
    applied: list[dict] = Field(default_factory=list)
    rejected: list[RejectedAction] = Field(default_factory=list)
    tasks_created: list[str] = Field(default_factory=list)
    issues_raised: list[str] = Field(default_factory=list)
    raw_reply: str | None = None
