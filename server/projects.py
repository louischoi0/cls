"""Project, project-agent and task rules (docs/PROJECTS.md FR-P1..FR-P3).

Everything a projectmanager is allowed to do goes through here, so the same
checks apply whether an action arrived from the operator's API call or from a
plan the manager produced. The manager gets no privileged path.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from .dispatcher import Dispatcher
from .logstore import LogStoreError, slugify
from .models import (
    DEFAULT_TOOL_POLICY,
    MAX_AGENTS_PER_PROJECT,
    AgentConfig,
    AgentRole,
    ProjectAgentCreate,
    ProjectAgentRecord,
    ProjectCreate,
    ProjectInfo,
    ProjectRecord,
    TaskRecord,
    runtime_name,
)
from .pool import AgentPool
from .registry import RegistryError
from .runner import Job, RunResult
from .store import ProjectStore, StoreError

log = logging.getLogger("cc_automation.projects")

OVERVIEW_FILE = "overview.md"
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

        try:
            project = self.store.create_project(project_id, spec.name, root, policy)
        except StoreError as exc:
            raise ProjectError(409, str(exc)) from exc
        return project

    def require_project(self, project_id: str) -> ProjectRecord:
        project = self.store.get_project(project_id)
        if project is None:
            raise ProjectError(404, f"unknown project {project_id!r}")
        return project

    def describe(self, project: ProjectRecord) -> ProjectInfo:
        agents = self.store.list_agents(project.id)
        manager = next((a.name for a in agents if a.role == "manager"), None)
        open_tasks = len(self.store.list_tasks(project.id, status="queued")) + len(
            self.store.list_tasks(project.id, status="running")
        )
        return ProjectInfo(
            **project.model_dump(),
            agents=[a.name for a in agents],
            manager=manager,
            open_tasks=open_tasks,
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
        return record

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
        log.info("project %s: removed agent %s", project.id, name)
        return cancelled

    async def _unregister(self, runtime: str) -> None:
        try:
            await self.pool.remove(runtime)
        except RegistryError:
            log.warning("agent %s was not in the pool", runtime)

    # -- tasks ------------------------------------------------------------- #

    async def create_task(
        self,
        project: ProjectRecord,
        agent_name: str,
        title: str,
        text: str,
        created_by: str,
    ) -> TaskRecord:
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
        )
        await self.dispatcher.enqueue(
            record.runtime_name,
            Job(
                message_id=message_id,
                agent=record.runtime_name,
                text=f"[task {task_id}] {title}\n\n{text}",
                topic=project.id,
                task_id=task_id,
            ),
        )
        return task

    def cancel_task(self, task_id: str) -> TaskRecord:
        task = self.store.get_task(task_id)
        if task is None:
            raise ProjectError(404, f"unknown task {task_id!r}")
        if not self.store.cancel_if_queued(task_id):
            raise ProjectError(
                409, f"task is {task.status}; only a queued task can be cancelled"
            )
        return self.store.get_task(task_id)

    # -- JobObserver (docs/PROJECTS.md FR-P3) ------------------------------- #

    async def job_started(self, job: Job) -> bool:
        if job.task_id is None:
            return True
        task = self.store.get_task(job.task_id)
        if task is None or task.status == "cancelled":
            return False
        self.store.set_task_status(job.task_id, "running")
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
            await self.dispatcher.enqueue(
                task.agent,
                Job(
                    message_id=task.message_id,
                    agent=task.agent,
                    text=f"[task {task.id}] {task.title}\n\n{task.text}",
                    topic=task.project_id,
                    task_id=task.id,
                ),
            )
            log.info("re-enqueued task %s for %s", task.id, task.agent)

    def project_of(self, runtime: str) -> tuple[str, AgentRole] | None:
        record = self.store.get_agent_by_runtime(runtime)
        return (record.project_id, record.role) if record else None
