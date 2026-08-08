"""Project, project-agent and task rules (docs/PROJECTS.md FR-P1..FR-P3).

Everything a projectmanager is allowed to do goes through here, so the same
checks apply whether an action arrived from the operator's API call or from a
plan the manager produced. The manager gets no privileged path.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from . import clsdir
from .asks import ASK_PROTOCOL, MAX_ASKS, parse_asks
from .dispatcher import Dispatcher
from .docimport import DocItem, read_document
from .github import README_NAMES, GithubError, canonical_url, fetch_readme
from .logstore import LogStoreError, slugify
from .models import (
    utcnow,
    DEFAULT_TOOL_POLICY,
    FULL_TOOL_POLICY,
    MAX_AGENTS_PER_PROJECT,
    AgentConfig,
    AgentState,
    infer_activity,
    AgentRole,
    ProjectAgentCreate,
    ProjectAgentRecord,
    ProjectAgentUpdate,
    AgentActivity,
    IssueCreate,
    IssueRecord,
    MilestoneCreate,
    MilestoneImported,
    MilestoneImportResult,
    MilestoneProgress,
    MilestoneRecord,
    MilestoneUpdate,
    ProjectInsight,
    SearchHit,
    OverviewSyncResult,
    ProjectCreate,
    ProjectInfo,
    ProjectRecord,
    ProjectSettings,
    ProjectSettingsUpdate,
    TaskRecord,
    runtime_name,
)
from .pool import AgentPool
from .registry import RegistryError
from .runner import Job, RunResult
from .store import ProjectStore, StoreError

log = logging.getLogger("cc_automation.projects")

OVERVIEW_FILE = "overview.md"
#: Where design documents live when the caller does not say.
DEFAULT_IMPORT_PATHS = ("docs",)
#: Files that describe the project rather than a goal within it.
SKIP_IMPORT_NAMES = {"readme.md", "overview.md", "index.md", "claude.md"}
#: A milestone body is a document, but not an unbounded one.
MAX_IMPORT_BODY = 20_000
#: statuses that mean "an agent has this now"
ACTIVE_TASK_STATUSES = ("queued", "running")
#: where the push credential lives in the secret store
GITHUB_TOKEN_KEY = "github_token"
MAX_PROJECT_ID_LEN = 32


class ProjectError(Exception):
    """Carries the HTTP status the route should answer with."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


def tool_base(tool: str) -> str:
    """`Bash(ls *)` is a use of `Bash`; policy is written in base names."""
    return tool.split("(", 1)[0].strip()


class ProjectService:
    def __init__(
        self, store: ProjectStore, pool: AgentPool, dispatcher: Dispatcher, status
    ) -> None:
        self.store = store
        self.pool = pool
        self.dispatcher = dispatcher
        self.status = status

    # -- projects ---------------------------------------------------------- #

    def create_project(self, spec: ProjectCreate) -> ProjectRecord:
        try:
            project_id = slugify(spec.name)[:MAX_PROJECT_ID_LEN].strip("-")
        except LogStoreError as exc:
            raise ProjectError(422, str(exc)) from exc
        if not project_id:
            raise ProjectError(422, f"project name has no usable characters: {spec.name!r}")

        root = spec.root_dir.resolve()
        if not root.is_dir():
            raise ProjectError(422, f"root_dir is not a directory: {root}")

        policy = spec.tool_policy if spec.tool_policy is not None else list(DEFAULT_TOOL_POLICY)
        if not policy:
            raise ProjectError(422, "tool_policy must not be empty; agents would get no tools")

        link = None
        if spec.github_url and spec.github_url.strip():
            try:
                link = canonical_url(spec.github_url)
            except GithubError as exc:
                raise ProjectError(422, str(exc)) from exc

        try:
            project = self.store.create_project(project_id, spec.name, root, policy, link)
        except StoreError as exc:
            raise ProjectError(409, str(exc)) from exc
        return project

    # -- settings and credentials ------------------------------------------ #

    def settings(self, project: ProjectRecord) -> ProjectSettings:
        token = self.store.get_secret(project.id, GITHUB_TOKEN_KEY)
        return ProjectSettings(
            project_id=project.id,
            github_url=project.github_url,
            git_user_name=self.store.get_secret(project.id, "git_user_name") or "",
            git_user_email=self.store.get_secret(project.id, "git_user_email") or "",
            allow_push=self.store.get_secret(project.id, "allow_push") == "1",
            github_token_set=bool(token),
            # enough to tell two tokens apart, useless to anyone who reads it
            github_token_hint=f"…{token[-4:]}" if token and len(token) > 4 else "",
        )

    def update_settings(
        self, project: ProjectRecord, patch: ProjectSettingsUpdate
    ) -> ProjectSettings:
        if patch.github_url is not None:
            project = self.set_github(project, patch.github_url)
        for field in ("git_user_name", "git_user_email"):
            value = getattr(patch, field)
            if value is not None:
                self.store.set_secret(project.id, field, value.strip() or None)
        if patch.allow_push is not None:
            self.store.set_secret(project.id, "allow_push", "1" if patch.allow_push else None)
        if patch.github_token is not None:
            token = patch.github_token.strip()
            self.store.set_secret(project.id, GITHUB_TOKEN_KEY, token or None)
            log.info("project %s: github token %s", project.id,
                     "set" if token else "cleared")
        return self.settings(self.require_project(project.id))

    def agent_env(self, runtime: str) -> dict[str, str]:
        """Extra environment for one agent's `claude` subprocess.

        Git credentials go in the environment rather than the prompt or a file
        on disk: `GIT_CONFIG_*` installs a credential helper for github.com for
        the life of the process only.

        **This gives the agent the token.** An agent with Bash can read its own
        environment, so "the agent can push" and "the agent holds a credential
        that can push" are the same statement. The containment is the token's
        own scope — use a fine-grained PAT limited to this repository.
        """
        record = self.store.get_agent_by_runtime(runtime)
        if record is None:
            return {}
        project_id = record.project_id
        env: dict[str, str] = {}

        name = self.store.get_secret(project_id, "git_user_name")
        email = self.store.get_secret(project_id, "git_user_email")
        if name:
            env["GIT_AUTHOR_NAME"] = env["GIT_COMMITTER_NAME"] = name
        if email:
            env["GIT_AUTHOR_EMAIL"] = env["GIT_COMMITTER_EMAIL"] = email

        token = self.store.get_secret(project_id, GITHUB_TOKEN_KEY)
        if token and self.store.get_secret(project_id, "allow_push") == "1":
            env["GH_TOKEN"] = token          # the gh CLI reads this
            env["GIT_TERMINAL_PROMPT"] = "0"  # never hang waiting for a password
            env["GIT_CONFIG_COUNT"] = "1"
            env["GIT_CONFIG_KEY_0"] = "credential.https://github.com.helper"
            env["GIT_CONFIG_VALUE_0"] = (
                "!f() { echo username=x-access-token; echo password=$GH_TOKEN; }; f"
            )
        return env

    def set_github(self, project: ProjectRecord, github_url: str | None) -> ProjectRecord:
        """Set the repository link, or clear it with an empty string."""
        if github_url is None or not github_url.strip():
            self.store.set_project_github(project.id, None)
        else:
            try:
                link = canonical_url(github_url)
            except GithubError as exc:
                raise ProjectError(422, str(exc)) from exc
            self.store.set_project_github(project.id, link)
        return self.require_project(project.id)

    def set_tool_policy(
        self, project: ProjectRecord, tool_policy: list[str]
    ) -> ProjectRecord:
        """Change the ceiling on what this project's agents may be granted.

        Widening it grants nothing by itself — an agent's `allowed_tools` was
        resolved when it was created, so the new tool reaches agents made after
        this. Narrowing is refused when an agent already holds what would be
        taken away: the policy is meant to be the truth about the project, and
        one that its own agents violate is not.
        """
        policy = [t.strip() for t in tool_policy if t and t.strip()]
        if not policy:
            raise ProjectError(422, "tool_policy must not be empty; agents would get no tools")

        unknown = sorted({tool_base(t) for t in policy} - set(FULL_TOOL_POLICY))
        if unknown:
            raise ProjectError(
                422, f"not tools this server can grant: {unknown}; "
                     f"known tools are {sorted(FULL_TOOL_POLICY)}"
            )

        allowed = {tool_base(t) for t in policy}
        for record in self.store.list_agents(project.id):
            held = {tool_base(t) for t in record.config.allowed_tools}
            lost = sorted(held - allowed)
            if lost:
                raise ProjectError(
                    409,
                    f"agent {record.name!r} already holds {lost}; remove or "
                    f"recreate it before narrowing the policy",
                )

        self.store.set_project_tool_policy(project.id, policy)
        log.info("project %s: tool_policy is now %s", project.id, policy)
        return self.require_project(project.id)

    async def sync_overview(
        self, project: ProjectRecord, *, overwrite: bool = False
    ) -> OverviewSyncResult:
        """Seed `overview.md` from the project's README.

        The working copy wins over the network: if `root_dir` already holds a
        README, that is the one the manager should plan from, and it works for a
        private repository where the unauthenticated fetch below cannot.
        """
        path = self.overview_path(project)
        if path.exists() and not overwrite:
            return OverviewSyncResult(
                project_id=project.id, written=False,
                detail="overview.md already exists; pass overwrite to replace it",
            )

        local = self._local_readme(project)
        if local is not None:
            name, text = local
            source = f"{project.root_dir}/{name}"
        elif project.github_url:
            try:
                name, text = await fetch_readme(project.github_url)
            except GithubError as exc:
                raise ProjectError(422, str(exc)) from exc
            source = f"{project.github_url} ({name})"
        else:
            return OverviewSyncResult(
                project_id=project.id, written=False,
                detail="no README in the project root and no GitHub link set",
            )

        self.write_overview(project, text)
        log.info("project %s: overview.md seeded from %s", project.id, source)
        return OverviewSyncResult(
            project_id=project.id, written=True, source=source,
            bytes=len(text.encode("utf-8")),
            detail=f"overview.md written from {source}",
        )

    @staticmethod
    def _local_readme(project: ProjectRecord) -> tuple[str, str] | None:
        root = Path(project.root_dir)
        for name in README_NAMES:
            candidate = root / name
            if candidate.is_file():
                try:
                    return name, candidate.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    log.warning("could not read %s: %s", candidate, exc)
        return None

    def require_project(self, project_id: str) -> ProjectRecord:
        project = self.store.get_project(project_id)
        if project is None:
            raise ProjectError(404, f"unknown project {project_id!r}")
        return project

    def describe(self, project: ProjectRecord) -> ProjectInfo:
        agents = self.store.list_agents(project.id)
        milestones = self.store.list_milestones(project.id)
        manager = next((a.name for a in agents if a.role == "manager"), None)
        open_tasks = sum(
            len(self.store.list_tasks(project.id, status=s, limit=1000))
            for s in ACTIVE_TASK_STATUSES
        )
        return ProjectInfo(
            **project.model_dump(),
            agents=[a.name for a in agents],
            manager=manager,
            open_tasks=open_tasks,
            backlog_tasks=len(
                self.store.list_tasks(project.id, status="backlog", limit=1000)
            ),
            open_issues=len(self.store.list_issues(project.id, status="open")),
            milestones=len(milestones),
            milestones_done=sum(1 for m in milestones if m.status == "done"),
        )

    async def delete_project(self, project: ProjectRecord) -> None:
        running = self.store.list_tasks(project.id, status="running")
        if running:
            # Cancelling a worker mid-run abandons its `claude` subprocess
            # rather than killing it, so the project outlives the request that
            # would have deleted it.
            raise ProjectError(
                409, f"task {running[0].id} is still running; wait for it or let it time out"
            )
        for record in self.store.list_agents(project.id):
            await self._unregister(record.runtime_name)
        # Rows cascade from the project; files under root_dir are never touched.
        self.store.delete_project(project.id)

    def overview_path(self, project: ProjectRecord) -> Path:
        return Path(project.root_dir) / OVERVIEW_FILE

    def read_overview(self, project: ProjectRecord) -> str:
        path = self.overview_path(project)
        if not path.is_file():
            raise ProjectError(404, f"{OVERVIEW_FILE} does not exist in {project.root_dir}")
        return path.read_text(encoding="utf-8")

    def write_overview(self, project: ProjectRecord, content: str) -> Path:
        path = self.overview_path(project)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    # -- agents ------------------------------------------------------------ #

    def build_config(self, project: ProjectRecord, spec: ProjectAgentCreate) -> AgentConfig:
        """Validate one agent spec against the project's boundary."""
        root = Path(project.root_dir).resolve()
        cwd = (root / spec.cwd).resolve() if spec.cwd else root
        if not cwd.is_relative_to(root):
            raise ProjectError(422, f"cwd escapes the project root: {spec.cwd!r}")
        if not cwd.is_dir():
            raise ProjectError(422, f"cwd does not exist: {cwd}")

        # An empty allowed_tools list means "no --allowedTools flag", which the
        # CLI reads as *every* tool. Project agents fall back to the project
        # policy instead, never to unrestricted.
        tools = spec.allowed_tools or list(project.tool_policy)
        allowed = {tool_base(t) for t in project.tool_policy}
        outside = sorted({tool_base(t) for t in tools} - allowed)
        if outside:
            raise ProjectError(
                422,
                f"tools outside the project tool_policy: {outside}; "
                f"policy is {project.tool_policy}",
            )

        name = runtime_name(project.id, spec.name)
        try:
            return AgentConfig(
                name=name,
                tags=[],
                cwd=cwd,
                system_prompt=spec.system_prompt,
                allowed_tools=tools,
                permission_mode=spec.permission_mode,
                model=spec.model,
                max_budget_usd=spec.max_budget_usd,
                timeout_s=spec.timeout_s,
            )
        except ValueError as exc:
            raise ProjectError(422, f"invalid agent: {exc}") from exc

    def add_agent(
        self, project: ProjectRecord, spec: ProjectAgentCreate
    ) -> ProjectAgentRecord:
        existing = self.store.list_agents(project.id)
        if len(existing) >= MAX_AGENTS_PER_PROJECT:
            raise ProjectError(
                409, f"project already has {MAX_AGENTS_PER_PROJECT} agents"
            )

        config = self.build_config(project, spec)
        if config.name in self.pool.registry.by_name:
            raise ProjectError(409, f"runtime agent name {config.name!r} is taken")

        try:
            record = self.store.add_agent(project.id, spec.name, spec.role, config)
        except StoreError as exc:
            raise ProjectError(409, str(exc)) from exc

        try:
            self.pool.add(config, extra_tags=[f"project:{project.id}"])
        except RegistryError as exc:
            self.store.delete_agent(project.id, spec.name)
            raise ProjectError(409, str(exc)) from exc
        log.info("project %s: added agent %s (%s)", project.id, spec.name, spec.role)
        self.sync_agent(config.name)
        return record

    def update_agent(
        self, project: ProjectRecord, name: str, patch: ProjectAgentUpdate
    ) -> ProjectAgentRecord:
        """Change an agent's settings without recreating it.

        Recreating one was the only way to change a budget, and it is a bad way:
        the runtime name is what its session is filed under, so a rebuild that
        renamed anything would strand the conversation the agent depends on.

        The new settings go through exactly the checks a new agent's would —
        `build_config` is the one gate — so a patch cannot grant tools the
        project's policy forbids. A run already in flight finishes on the old
        settings; the next job off the queue uses these.
        """
        record = self.require_agent(project, name)
        current = record.config
        root = Path(project.root_dir).resolve()
        cwd = Path(current.cwd).resolve()

        def pick(field: str, fallback):
            value = getattr(patch, field)
            return fallback if value is None else value

        spec = ProjectAgentCreate(
            name=record.name,
            role=record.role,
            # "" clears these back to the default; None left them alone above.
            system_prompt=(pick("system_prompt", current.system_prompt) or None),
            allowed_tools=pick("allowed_tools", current.allowed_tools),
            permission_mode=pick("permission_mode", current.permission_mode),
            model=(pick("model", current.model) or None),
            max_budget_usd=pick("max_budget_usd", current.max_budget_usd),
            timeout_s=pick("timeout_s", current.timeout_s),
            # Never patched, only carried: an agent works where its project is.
            cwd=str(cwd.relative_to(root)) if cwd != root else None,
        )
        config = self.build_config(project, spec)

        self.store.update_agent_config(project.id, name, config)
        self.pool.reconfigure(config)
        self.sync_agent(config.name)
        log.info("project %s: reconfigured agent %s", project.id, name)
        return self.require_agent(project, name)

    def require_agent(self, project: ProjectRecord, name: str) -> ProjectAgentRecord:
        record = self.store.get_agent(project.id, name)
        if record is None:
            raise ProjectError(404, f"unknown agent {name!r} in project {project.id!r}")
        return record

    async def delete_agent(self, project: ProjectRecord, name: str) -> int:
        record = self.require_agent(project, name)
        running = self.store.list_tasks(
            project.id, status="running", agent=record.runtime_name
        )
        if running:
            raise ProjectError(
                409, f"agent {name!r} has a running task ({running[0].id}); wait for it"
            )
        cancelled = self.store.cancel_queued_for_agent(
            record.runtime_name, f"agent {name!r} was deleted"
        )
        await self._unregister(record.runtime_name)
        self.store.delete_agent(project.id, name)
        self._forget_agent(project, name)
        log.info("project %s: removed agent %s", project.id, name)
        return cancelled

    async def _unregister(self, runtime: str) -> None:
        try:
            await self.pool.remove(runtime)
        except RegistryError:
            log.warning("agent %s was not in the pool", runtime)

    # -- the cls/ mirror ---------------------------------------------------- #
    # Every state change an agent goes through is written back into the project
    # it belongs to, so the repository says who is doing what without anyone
    # having to ask the server. See `clsdir` for why it is a mirror only.

    def sync_agent(self, runtime: str) -> None:
        """Rewrite one agent's snapshot under its project's `cls/`."""
        try:
            record = self.store.get_agent_by_runtime(runtime)
            if record is None:
                return  # an agents.yaml agent belongs to no project directory
            project = self.store.get_project(record.project_id)
            if project is None:
                return
            clsdir.write(project.root_dir, record.name, self.agent_state(runtime))
        except Exception:
            # A snapshot that cannot be written must not fail the operation that
            # changed the state, nor take down the worker loop.
            log.exception("could not write the cls snapshot for %s", runtime)

    def sync_project(self, project: ProjectRecord) -> None:
        for record in self.store.list_agents(project.id):
            self.sync_agent(record.runtime_name)

    def _forget_agent(self, project: ProjectRecord, name: str) -> None:
        try:
            clsdir.remove(project.root_dir, name)
        except Exception:
            log.exception("could not remove the cls snapshot for %s", name)

    # -- tasks ------------------------------------------------------------- #

    async def create_task(
        self,
        project: ProjectRecord,
        agent_name: str | None,
        title: str,
        text: str,
        created_by: str,
        branch: str | None = None,
        milestone_id: str | None = None,
    ) -> TaskRecord:
        """Create a task, and queue it if it names an agent.

        No agent means backlog: the work is written down but nobody has it, so
        nothing is dispatched and nothing is spent. `assign_task` is what turns
        one of those into a running job.
        """
        if milestone_id:
            self.require_milestone(project, milestone_id)
        if agent_name is None:
            return self.store.create_task(
                task_id=uuid.uuid4().hex[:16],
                project_id=project.id,
                agent=None,
                title=title,
                text=text,
                created_by=created_by,
                message_id=None,
                branch=branch,
                milestone_id=milestone_id,
                status="backlog",
            )

        record = self.require_agent(project, agent_name)
        if record.role == "manager" and created_by == "manager":
            raise ProjectError(422, "the projectmanager may not assign tasks to itself")

        task_id = uuid.uuid4().hex[:16]
        message_id = uuid.uuid4().hex[:16]
        self.status.create(
            message_id, project.id, [f"project:{project.id}"], [record.runtime_name]
        )
        task = self.store.create_task(
            task_id=task_id,
            project_id=project.id,
            agent=record.runtime_name,
            title=title,
            text=text,
            created_by=created_by,
            message_id=message_id,
            branch=branch,
            milestone_id=milestone_id,
        )
        await self._enqueue(record.runtime_name, task)
        return task

    async def assign_task(
        self,
        project: ProjectRecord,
        task_id: str,
        agent_name: str,
        branch: str | None = None,
        milestone_id: str | None = None,
    ) -> TaskRecord:
        """Hand a backlog task to an agent, which queues it."""
        task = self.store.get_task(task_id)
        if task is None or task.project_id != project.id:
            raise ProjectError(404, f"unknown task {task_id!r} in this project")
        if task.status != "backlog":
            raise ProjectError(
                409, f"task is {task.status}; only a backlog task can be assigned"
            )
        record = self.require_agent(project, agent_name)
        if milestone_id:
            self.require_milestone(project, milestone_id)

        message_id = uuid.uuid4().hex[:16]
        if not self.store.assign_task(
            task_id, record.runtime_name, message_id, branch, milestone_id
        ):
            raise ProjectError(409, "task left the backlog while it was being assigned")
        self.status.create(
            message_id, project.id, [f"project:{project.id}"], [record.runtime_name]
        )
        task = self.store.get_task(task_id)
        await self._enqueue(record.runtime_name, task)
        log.info("project %s: task %s assigned to %s", project.id, task_id, agent_name)
        return task

    @staticmethod
    def job_text(task: TaskRecord) -> str:
        """Everything the agent receives. It has no other context.

        The ask protocol rides along with the work rather than living in a
        system prompt, which an agent may not have been given one of.
        """
        return f"[task {task.id}] {task.title}\n\n{task.text}\n{ASK_PROTOCOL}"

    async def _enqueue(self, runtime: str, task: TaskRecord) -> None:
        await self.dispatcher.enqueue(
            runtime,
            Job(
                message_id=task.message_id,
                agent=runtime,
                text=self.job_text(task),
                topic=task.project_id,
                task_id=task.id,
                branch=task.branch,
            ),
        )
        self.sync_agent(runtime)

    def cancel_task(self, task_id: str) -> TaskRecord:
        task = self.store.get_task(task_id)
        if task is None:
            raise ProjectError(404, f"unknown task {task_id!r}")
        if not self.store.cancel_if_queued(task_id):
            raise ProjectError(
                409,
                f"task is {task.status}; only a queued or backlog task can be cancelled",
            )
        if task.agent:
            self.sync_agent(task.agent)
        return self.store.get_task(task_id)

    # -- issues ------------------------------------------------------------ #

    def raise_issue(
        self, project: ProjectRecord, spec: IssueCreate, created_by: str
    ) -> IssueRecord:
        """Record something that is stopping the project.

        The agent name is validated when given: an issue pointing at an agent
        that does not exist is worse than one pointing at nothing.
        """
        if spec.agent is not None:
            self.require_agent(project, spec.agent)
        milestone_id = spec.milestone_id
        if milestone_id:
            self.require_milestone(project, milestone_id)
        if spec.task_id is not None:
            task = self.store.get_task(spec.task_id)
            if task is None or task.project_id != project.id:
                raise ProjectError(404, f"unknown task {spec.task_id!r} in this project")
            # An issue out of a task belongs to that task's milestone; the
            # hierarchy is inherited rather than restated.
            milestone_id = milestone_id or task.milestone_id
        issue = self.store.create_issue(
            issue_id=uuid.uuid4().hex[:16],
            project_id=project.id,
            title=spec.title,
            body=spec.body,
            kind=spec.kind,
            created_by=created_by,
            agent=spec.agent,
            task_id=spec.task_id,
            branch=spec.branch,
            milestone_id=milestone_id,
        )
        log.info("project %s: issue raised (%s) %s", project.id, spec.kind, issue.id)
        return issue

    def close_issue(self, issue_id: str, resolution: str, dismiss: bool) -> IssueRecord:
        issue = self.store.get_issue(issue_id)
        if issue is None:
            raise ProjectError(404, f"unknown issue {issue_id!r}")
        status = "dismissed" if dismiss else "resolved"
        if not self.store.resolve_issue(issue_id, status, resolution):
            raise ProjectError(
                409, f"issue is {issue.status}; only an open issue can be closed"
            )
        return self.store.get_issue(issue_id)

    # -- milestones --------------------------------------------------------- #
    # A milestone is the operator's statement of intent. Agents fill it with
    # tasks; only a person creates, edits or closes one.

    def create_milestone(
        self, project: ProjectRecord, spec: MilestoneCreate
    ) -> MilestoneRecord:
        existing = self.store.list_milestones(project.id)
        return self.store.create_milestone(
            milestone_id=uuid.uuid4().hex[:16],
            project_id=project.id,
            title=spec.title,
            body=spec.body,
            target=spec.target,
            branch=spec.branch,
            # appended to the roadmap rather than dropped into it
            position=max((m.position for m in existing), default=-1) + 1,
        )

    def import_milestones(
        self,
        project: ProjectRecord,
        paths: list[str],
        dry_run: bool = False,
        tasks: bool = True,
        issues: bool = True,
    ) -> MilestoneImportResult:
        """Turn design documents into milestones, tasks and issues.

        A path may be a file or a directory; a directory is scanned for `*.md`,
        one level deep. Nothing given means `docs/`, which is where this kind of
        document usually lives.

        Each document becomes one milestone. Its work lists become **backlog**
        tasks under that milestone — rows with no agent and no queue entry, so an
        import costs nothing until someone assigns them — and its open questions
        become open issues. `docimport` decides which list is which.

        A document already imported is skipped rather than duplicated — the
        milestone remembers the file it came from, so running this again after
        adding a doc picks up only the new one. Tasks and issues therefore come
        in exactly once, with the milestone they hang off.
        """
        root = Path(project.root_dir).resolve()
        wanted = [p for p in (paths or DEFAULT_IMPORT_PATHS) if p.strip()]
        existing = {
            m.source for m in self.store.list_milestones(project.id) if m.source
        }
        titles = {m.title for m in self.store.list_milestones(project.id)}

        files: list[Path] = []
        scanned: list[str] = []
        for entry in wanted:
            target = (root / entry).resolve()
            if not target.is_relative_to(root):
                raise ProjectError(422, f"path escapes the project root: {entry!r}")
            if target.is_dir():
                scanned.append(f"{entry}/")
                files.extend(sorted(target.glob("*.md")))
            elif target.is_file():
                scanned.append(entry)
                files.append(target)
            else:
                raise ProjectError(404, f"no such file or directory: {entry!r}")

        result = MilestoneImportResult(
            project_id=project.id, scanned=scanned, dry_run=dry_run
        )
        position = max(
            (m.position for m in self.store.list_milestones(project.id)), default=-1
        )
        for path in files:
            rel = str(path.relative_to(root))
            if path.name.lower() in SKIP_IMPORT_NAMES:
                continue  # the brief itself is not a goal
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                result.skipped.append(
                    MilestoneImported(source=rel, title=path.stem, skipped=str(exc))
                )
                continue
            title = _document_title(text, path)
            if rel in existing:
                result.skipped.append(
                    MilestoneImported(source=rel, title=title, skipped="already imported")
                )
                continue
            if title in titles:
                result.skipped.append(
                    MilestoneImported(
                        source=rel, title=title,
                        skipped="a milestone with this title already exists",
                    )
                )
                continue

            reading = read_document(text)
            wanted_tasks = reading.tasks if tasks else []
            wanted_issues = reading.issues if issues else []
            entry_result = MilestoneImported(
                source=rel, title=title,
                tasks=len(wanted_tasks), issues=len(wanted_issues),
            )
            if not dry_run:
                position += 1
                milestone = self.store.create_milestone(
                    milestone_id=uuid.uuid4().hex[:16],
                    project_id=project.id,
                    title=title,
                    body=text[:MAX_IMPORT_BODY],
                    target="",
                    branch=None,
                    position=position,
                    source=rel,
                )
                entry_result.milestone_id = milestone.id
                titles.add(title)
                result.tasks_created.extend(
                    self._import_tasks(project, milestone, rel, wanted_tasks)
                )
                result.issues_created.extend(
                    self._import_issues(project, milestone, rel, reading, wanted_issues)
                )
            if reading.truncated:
                log.warning(
                    "project %s: %s has more work items than one import takes",
                    project.id, rel,
                )
            result.imported.append(entry_result)

        verb = "would import" if dry_run else "imported"
        counts = sum(e.tasks for e in result.imported), sum(
            e.issues for e in result.imported
        )
        result.detail = (
            f"{verb} {len(result.imported)} milestone(s), {counts[0]} task(s) and "
            f"{counts[1]} issue(s) from {', '.join(scanned) or 'nothing'}"
            + (f", skipped {len(result.skipped)}" if result.skipped else "")
        )
        log.info("project %s: %s", project.id, result.detail)
        return result

    def _import_tasks(
        self,
        project: ProjectRecord,
        milestone: MilestoneRecord,
        source: str,
        items: list[DocItem],
    ) -> list[str]:
        """Write a document's work list as backlog tasks under its milestone."""
        created: list[str] = []
        for item in items:
            task = self.store.create_task(
                task_id=uuid.uuid4().hex[:16],
                project_id=project.id,
                agent=None,
                title=item.title,
                text=_provenance(item, source),
                created_by="import",
                message_id=None,
                milestone_id=milestone.id,
                status="backlog",
            )
            created.append(task.id)
        return created

    def _import_issues(
        self,
        project: ProjectRecord,
        milestone: MilestoneRecord,
        source: str,
        reading,
        items: list[DocItem],
    ) -> list[str]:
        """Write a document's open questions and risks as open issues."""
        created: list[str] = []
        for item in items:
            issue = self.store.create_issue(
                issue_id=uuid.uuid4().hex[:16],
                project_id=project.id,
                title=item.title,
                body=_provenance(item, source),
                kind=reading.kind_of(item),
                created_by="import",
                milestone_id=milestone.id,
            )
            created.append(issue.id)
        return created

    def require_milestone(
        self, project: ProjectRecord, milestone_id: str
    ) -> MilestoneRecord:
        milestone = self.store.get_milestone(milestone_id)
        if milestone is None or milestone.project_id != project.id:
            raise ProjectError(404, f"unknown milestone {milestone_id!r} in this project")
        return milestone

    def update_milestone(
        self, project: ProjectRecord, milestone_id: str, patch: MilestoneUpdate
    ) -> MilestoneRecord:
        self.require_milestone(project, milestone_id)
        self.store.update_milestone(
            milestone_id, **patch.model_dump(exclude_none=True)
        )
        return self.store.get_milestone(milestone_id)

    def delete_milestone(self, project: ProjectRecord, milestone_id: str) -> None:
        self.require_milestone(project, milestone_id)
        self.store.delete_milestone(milestone_id)

    def progress(
        self, milestone: MilestoneRecord | None, tasks, issues, project_id: str
    ) -> MilestoneProgress:
        """Roll up the work under one milestone (or the unassigned pile)."""
        mid = milestone.id if milestone else None
        mine = [t for t in tasks if t.milestone_id == mid]
        placeholder = milestone or MilestoneRecord(
            id="", project_id=project_id, title="Unassigned", body="",
            target="", status="active", created_by="user", position=9999,
            created_at=utcnow(),
        )
        return MilestoneProgress(
            milestone=placeholder,
            tasks_total=len(mine),
            tasks_done=sum(1 for t in mine if t.status == "done"),
            tasks_failed=sum(1 for t in mine if t.status == "failed"),
            tasks_open=sum(1 for t in mine if t.status in ACTIVE_TASK_STATUSES),
            tasks_backlog=sum(1 for t in mine if t.status == "backlog"),
            issues_open=sum(
                1 for i in issues if i.milestone_id == mid and i.status == "open"
            ),
            cost_usd=round(sum(t.cost_usd or 0.0 for t in mine), 4),
        )

    def insight(self, project: ProjectRecord) -> ProjectInsight:
        """Everything the project page needs to say how the project is going."""
        milestones = self.store.list_milestones(project.id)
        tasks = self.store.list_tasks(project.id, limit=1000)
        issues = self.store.list_issues(project.id, limit=1000)
        agents = self.store.list_agents(project.id)

        by_agent: dict[str, AgentActivity] = {}
        for record in agents:
            worker = self.pool.workers.get(record.runtime_name)
            by_agent[record.runtime_name] = AgentActivity(
                agent=record.name,
                role=record.role,
                busy=bool(worker and worker.busy),
                queue_depth=self.dispatcher.depth(record.runtime_name)
                if record.runtime_name in self.pool.registry.by_name else 0,
            )
        for task in tasks:
            activity = by_agent.get(task.agent)
            if activity is None:
                continue  # an agent that has since been removed
            activity.cost_usd = round(activity.cost_usd + (task.cost_usd or 0.0), 4)
            if task.status == "done":
                activity.tasks_done += 1
            elif task.status == "failed":
                activity.tasks_failed += 1
            elif task.status in ("queued", "running"):
                activity.tasks_open += 1

        unassigned = self.progress(None, tasks, issues, project.id)
        recent = [
            SearchHit(
                type="task", id=t.id, project_id=project.id, title=t.title,
                snippet=(t.result or t.error or t.text or "")[:200],
                status=t.status, agent=t.agent, branch=t.branch,
                created_at=t.created_at,
            )
            for t in tasks[:8]
        ] + [
            SearchHit(
                type="issue", id=i.id, project_id=project.id, title=i.title,
                snippet=i.body[:200], status=i.status, kind=i.kind,
                agent=i.agent, branch=i.branch, created_at=i.created_at,
            )
            for i in issues[:8]
        ]
        recent.sort(key=lambda h: h.created_at, reverse=True)

        return ProjectInsight(
            project_id=project.id,
            name=project.name,
            milestones=[self.progress(m, tasks, issues, project.id) for m in milestones],
            unassigned=unassigned if unassigned.tasks_total else None,
            tasks_by_status=_tally(t.status for t in tasks),
            issues_by_status=_tally(i.status for i in issues),
            open_issues_by_kind=_tally(i.kind for i in issues if i.status == "open"),
            agents=sorted(by_agent.values(), key=lambda a: (a.role != "manager", a.agent)),
            branches=sorted({t.branch for t in tasks if t.branch}
                            | {i.branch for i in issues if i.branch}),
            cost_usd=round(sum(t.cost_usd or 0.0 for t in tasks), 4),
            recent=recent[:10],
        )

    def agent_state(self, runtime: str) -> AgentState:
        """One agent's live state, whether it came from a project or the yaml.

        Reads the worker and the queue for "now", and the task rows for "so
        far"; an `agents.yaml` agent simply has no rows and reports zeroes.
        """
        config = self.pool.registry.by_name.get(runtime)
        if config is None:
            raise ProjectError(404, f"unknown agent {runtime!r}")
        owner = self.store.get_agent_by_runtime(runtime)
        worker = self.pool.workers.get(runtime)

        tasks = self.store.list_tasks(agent=runtime, limit=200)
        running = next((t for t in tasks if t.status == "running"), None)
        finished = [t for t in tasks if t.finished_at]

        # What it is doing right now. The job in flight is the authority — a
        # `running` row can outlive its worker after a crash, and a planning
        # round has no task row at all.
        job = worker.current if worker else None
        queue_depth = self.dispatcher.depth(runtime)
        activity, detail, working_on, since = "idle", "", None, None
        running_message_id = job.message_id if job is not None else None
        if job is not None:
            if job.task_id:
                task = self.store.get_task(job.task_id) or running
                working_on = job.task_id
                detail = task.title if task else job.text[:120]
                activity = infer_activity(detail, task.text if task else "")
                since = task.started_at if task else None
            elif job.topic.endswith("-plan"):
                activity, detail = "planning", "planning round"
            else:
                activity, detail = "working", job.text[:120]
        elif queue_depth:
            activity, detail = "queued", f"{queue_depth} waiting"

        open_issues = list(
            self.store.list_issues(agent=runtime, status="open", limit=20)
        )
        subject, kind, subject_id, milestone = self._subject(
            running or next((t for t in tasks if t.status != "cancelled"), None),
            open_issues,
        )

        return AgentState(
            name=runtime,
            project=owner.project_id if owner else None,
            role=owner.role if owner else None,
            cwd=str(config.cwd),
            tags=config.tags,
            model=config.model,
            allowed_tools=config.allowed_tools,
            permission_mode=config.permission_mode,
            system_prompt=config.system_prompt,
            max_budget_usd=config.max_budget_usd,
            timeout_s=config.timeout_s,
            busy=bool(worker and worker.busy),
            activity=activity,
            working_on=working_on,
            running_message_id=running_message_id,
            subject=subject,
            subject_kind=kind,
            subject_id=subject_id,
            milestone=milestone,
            waiting=any(i.created_by == "agent" for i in open_issues),
            activity_detail=detail,
            activity_since=since,
            queue_depth=queue_depth,
            session_id=self.sessions_for(runtime),
            running=running,
            queued=[t for t in tasks if t.status == "queued"],
            recent=[t for t in tasks if t.status in ("done", "failed", "cancelled")][:10],
            open_issues=open_issues,
            tasks_done=sum(1 for t in tasks if t.status == "done"),
            tasks_failed=sum(1 for t in tasks if t.status == "failed"),
            cost_usd=round(sum(t.cost_usd or 0.0 for t in tasks), 4),
            last_active=max((t.finished_at for t in finished), default=None),
        )

    def _subject(self, task, open_issues) -> tuple[str | None, str | None, str | None, str | None]:
        """What a session is *for*: `(subject, kind, id, milestone)`.

        A question the operator has not answered outranks the work, because it
        is the reason the work stopped. Otherwise it is the task in flight, or
        the last one — a session that has gone quiet is still about something.
        """
        asked = next((i for i in open_issues if i.created_by == "agent"), None)
        if asked is not None:
            milestone = self.store.get_milestone(asked.milestone_id) if asked.milestone_id else None
            return asked.title, "issue", asked.id, milestone.title if milestone else None
        if task is None:
            return None, None, None, None
        milestone = self.store.get_milestone(task.milestone_id) if task.milestone_id else None
        return task.title, "task", task.id, milestone.title if milestone else None

    def sessions_for(self, runtime: str) -> str | None:
        return self.pool.sessions.get(runtime)

    # -- JobObserver (docs/PROJECTS.md FR-P3) ------------------------------- #

    async def job_started(self, job: Job) -> bool:
        if job.task_id is None:
            self.sync_agent(job.agent)  # a planning round is still activity
            return True
        task = self.store.get_task(job.task_id)
        if task is None or task.status == "cancelled":
            return False
        self.store.set_task_status(job.task_id, "running")
        self.sync_agent(job.agent)
        return True

    async def job_finished(self, job: Job, result: RunResult) -> None:
        if job.task_id is None:
            return
        self.store.set_task_status(
            job.task_id,
            "done" if result.ok else "failed",
            result=result.result_text if result.ok else None,
            error=None if result.ok else result.result_text,
            cost_usd=result.cost_usd,
        )
        if not result.ok:
            self._raise_crash_issue(job, result)
        else:
            self._raise_asks(job, result)

    def worker_idle(self, agent: str) -> None:
        # Deliberately not in `job_finished`: the worker still holds the job
        # there, so a snapshot written then would record the agent as busy with
        # something it has already finished.
        self.sync_agent(agent)

    def _raise_asks(self, job: Job, result: RunResult) -> None:
        """Turn a worker's questions into open issues on the board.

        A run cannot wait for an answer, so this is how one gets asked at all:
        the agent finishes, the question stays. The operator answers by resolving
        the issue, and the manager reads answered issues on its next round.
        """
        asks, too_many = parse_asks(result.result_text)
        if too_many:
            log.warning(
                "agent %s asked more than %d questions in one run; kept the first %d",
                job.agent, MAX_ASKS, MAX_ASKS,
            )
        if not asks:
            return
        task = self.store.get_task(job.task_id)
        if task is None:
            return
        for ask in asks:
            try:
                issue = self.store.create_issue(
                    issue_id=uuid.uuid4().hex[:16],
                    project_id=task.project_id,
                    title=ask.title,
                    body=ask.body,
                    kind="decision",
                    created_by="agent",
                    agent=job.agent,
                    task_id=task.id,
                    branch=task.branch,
                    milestone_id=task.milestone_id,
                )
                log.info(
                    "project %s: %s asked a question (%s)",
                    task.project_id, job.agent, issue.id,
                )
            except Exception:
                # Bookkeeping must never take down the worker loop.
                log.exception("could not record a question from %s", job.agent)

    def _raise_crash_issue(self, job: Job, result: RunResult) -> None:
        """A failed run is exactly the "crash unexpected" case an issue is for.

        Raised here rather than left for someone to notice: a task that failed
        is blocking whatever depended on it, and the manager reads open issues
        on its next planning round.
        """
        task = self.store.get_task(job.task_id)
        if task is None:
            return
        try:
            self.store.create_issue(
                issue_id=uuid.uuid4().hex[:16],
                project_id=task.project_id,
                title=f"Task failed: {task.title}",
                body=result.result_text[:4000],
                kind="crash",
                created_by="system",
                agent=job.agent,
                task_id=task.id,
                branch=task.branch,
                milestone_id=task.milestone_id,
            )
        except Exception:
            # Bookkeeping must never take down the worker loop.
            log.exception("could not raise a crash issue for task %s", task.id)

    # -- startup ----------------------------------------------------------- #

    async def restore(self) -> None:
        """Bring the durable state back after a restart (docs/PROJECTS.md FR-P3)."""
        orphaned = self.store.fail_running("server restarted mid-run")
        if orphaned:
            log.warning("marked %d task(s) failed: interrupted by a restart", orphaned)

        for record in self.store.list_agents():
            try:
                self.pool.add(record.config, extra_tags=[f"project:{record.project_id}"])
            except RegistryError as exc:
                # A stored agent whose runtime name a yaml agent has since taken.
                # Refusing to boot over it would strand every other project.
                log.error("could not restore agent %s: %s", record.runtime_name, exc)

        for task in self.store.list_tasks(status="queued"):
            if task.agent not in self.pool.registry.by_name:
                self.store.set_task_status(
                    task.id, "cancelled", error="its agent no longer exists"
                )
                continue
            self.status.create(
                task.message_id, task.project_id, [f"project:{task.project_id}"], [task.agent]
            )
            await self._enqueue(task.agent, task)
            log.info("re-enqueued task %s for %s", task.id, task.agent)

        # A restart is itself a state change: the snapshots on disk describe a
        # server that is no longer running until they are rewritten.
        for record in self.store.list_agents():
            self.sync_agent(record.runtime_name)

    def project_of(self, runtime: str) -> tuple[str, AgentRole] | None:
        record = self.store.get_agent_by_runtime(runtime)
        return (record.project_id, record.role) if record else None


def _tally(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1
    return counts


def _provenance(item: DocItem, source: str) -> str:
    """The item's own words, plus where in which file they came from.

    An imported task is read by an agent that has never seen the document, so
    the pointer back to it is the difference between a usable instruction and a
    fragment of a sentence.
    """
    where = f"{source} § {item.section}" if item.section else source
    return f"{item.body}\n\n(imported from {where})"


def _document_title(text: str, path: Path) -> str:
    """The first Markdown heading, or the filename made readable."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            title = stripped.lstrip("#").strip()
            if title:
                return title[:200]
        if stripped:
            break  # a document that opens with prose has no heading to take
    return path.stem.replace("-", " ").replace("_", " ").strip().capitalize()
