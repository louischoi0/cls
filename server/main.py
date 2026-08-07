"""FastAPI app: auth, routes, and the worker lifecycle."""

from __future__ import annotations

import hmac
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .dispatcher import DispatchError, Dispatcher, resolve_targets
from .logstore import LogStore, LogStoreError, slugify, validate_date
from .models import (
    AgentInfo,
    MessageAccepted,
    MessageRequest,
    MessageStatusResponse,
    PlanRequest,
    PlanResult,
    ProjectAgentCreate,
    ProjectAgentRecord,
    ProjectCreate,
    ProjectInfo,
    StatusStore,
    TaskCreate,
    TaskRecord,
)
from .planner import run_plan
from .pool import AgentPool
from .projects import ProjectError, ProjectService
from .registry import Registry, load_registry
from .runner import AgentWorker, Job, SessionStore, resolve_claude_bin
from .store import ProjectStore, open_store

PROJECT_ROOT = Path(__file__).resolve().parent.parent

#: What the *deployed* server stores projects in — KDS, with SQLite taking over
#: if kds_server is down rather than the server refusing to boot. Only
#: `Config.from_env()` applies it: a `Config(...)` built in code stays on
#: SQLite, so the tests never depend on an engine being up.
DEFAULT_STORE_URL = "kds://127.0.0.1:15432?fallback=sqlite"

UNAUTHENTICATED_PATHS = frozenset({"/health", "/"})
# The console's own HTML/CSS/JS. A browser cannot put a header on a navigation,
# so the shell is served unauthenticated; it holds no secrets, and every call it
# makes carries the key the operator pasted into it.
UNAUTHENTICATED_PREFIXES = ("/web/",)

log = logging.getLogger("cc_automation")


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("message_id", "agent"):
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonLineFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)


@dataclass
class Config:
    home: Path = PROJECT_ROOT
    agents_file: Path | None = None
    logs_dir: Path | None = None
    state_dir: Path | None = None
    api_key: str | None = None
    api_key_file: Path = Path("~/.cc-automation/api_key")
    claude_bin: str | None = None
    start_workers: bool = True
    #: the console's static files; they ship with the code, not with `home`
    web_dir: Path | None = None
    #: `sqlite://<path>` or `kds://host:port[?fallback=sqlite]`
    store_url: str | None = None

    def __post_init__(self) -> None:
        self.home = Path(self.home).expanduser()
        self.agents_file = Path(self.agents_file or self.home / "agents.yaml").expanduser()
        self.logs_dir = Path(self.logs_dir or self.home / "logs").expanduser()
        self.state_dir = Path(self.state_dir or self.home / "state").expanduser()
        self.api_key_file = Path(self.api_key_file).expanduser()
        self.web_dir = Path(self.web_dir or PROJECT_ROOT / "web").expanduser()
        if self.store_url is None:
            self.store_url = f"sqlite://{self.state_dir / 'projects.db'}"

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        return cls(
            home=Path(env("CC_AUTOMATION_HOME", str(PROJECT_ROOT))),
            agents_file=Path(env("CC_AUTOMATION_AGENTS_FILE")) if env("CC_AUTOMATION_AGENTS_FILE") else None,
            logs_dir=Path(env("CC_AUTOMATION_LOGS_DIR")) if env("CC_AUTOMATION_LOGS_DIR") else None,
            state_dir=Path(env("CC_AUTOMATION_STATE_DIR")) if env("CC_AUTOMATION_STATE_DIR") else None,
            api_key=env("CC_AUTOMATION_API_KEY"),
            api_key_file=Path(env("CC_AUTOMATION_API_KEY_FILE", "~/.cc-automation/api_key")),
            claude_bin=env("CC_AUTOMATION_CLAUDE_BIN"),
            web_dir=Path(env("CC_AUTOMATION_WEB_DIR")) if env("CC_AUTOMATION_WEB_DIR") else None,
            store_url=env("CC_AUTOMATION_STORE", DEFAULT_STORE_URL),
        )

    def resolve_api_key(self) -> str:
        if self.api_key:
            return self.api_key.strip()
        if self.api_key_file.is_file():
            key = self.api_key_file.read_text(encoding="utf-8").strip()
            if key:
                return key
        raise RuntimeError(
            "no API key: set CC_AUTOMATION_API_KEY or write one to "
            f"{self.api_key_file}"
        )


@dataclass
class AppState:
    config: Config
    api_key: str
    registry: Registry
    dispatcher: Dispatcher
    sessions: SessionStore
    logstore: LogStore
    status: StatusStore
    store: ProjectStore
    pool: AgentPool = None  # type: ignore[assignment]
    projects: ProjectService = None  # type: ignore[assignment]

    @property
    def workers(self) -> dict[str, AgentWorker]:
        return self.pool.workers


def create_app(
    config: Config | None = None,
    worker_factory: Callable[..., AgentWorker] = AgentWorker,
) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Everything that can be wrong with the configuration is discovered
        # here, before the first request is served.
        registry = load_registry(config.agents_file)
        state = AppState(
            config=config,
            api_key=config.resolve_api_key(),
            registry=registry,
            dispatcher=Dispatcher(registry),
            sessions=SessionStore(config.state_dir / "sessions.json"),
            logstore=LogStore(config.logs_dir),
            status=StatusStore(),
            store=open_store(
                config.store_url, sqlite_path=config.state_dir / "projects.db"
            ),
        )
        claude_bin = config.claude_bin or (
            resolve_claude_bin() if config.start_workers else "claude"
        )
        state.pool = AgentPool(
            registry=registry,
            dispatcher=state.dispatcher,
            sessions=state.sessions,
            logstore=state.logstore,
            status=state.status,
            claude_bin=claude_bin,
            worker_factory=worker_factory,
            start_workers=config.start_workers,
        )
        # The service is the pool's observer, and the pool is the service's way
        # to add agents; the cycle is closed here rather than in either one.
        state.projects = ProjectService(
            state.store, state.pool, state.dispatcher, state.status
        )
        state.pool.observer = state.projects
        for agent in registry.all_agents():
            state.pool.start(agent)
        await state.projects.restore()

        app.state.cc = state
        log.info(
            "started with agents: %s (store: %s)",
            ", ".join(registry.names), state.store.backend,
        )
        try:
            yield
        finally:
            await state.pool.shutdown()
            state.store.close()

    app = FastAPI(title="Claude Code Automation Server", version="1.0", lifespan=lifespan)

    @app.middleware("http")
    async def require_api_key(request: Request, call_next) -> Response:
        path = request.url.path
        if path in UNAUTHENTICATED_PATHS or path.startswith(UNAUTHENTICATED_PREFIXES):
            return await call_next(request)
        presented = request.headers.get("X-API-Key", "")
        expected = request.app.state.cc.api_key
        if not hmac.compare_digest(presented, expected):
            return JSONResponse({"detail": "invalid or missing X-API-Key"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health(request: Request) -> dict:
        # `store` is here so a silent fallback is not silent: if kds_server was
        # down at boot, this is where you see that sqlite took over.
        return {"status": "ok", "store": request.app.state.cc.store.backend}

    @app.exception_handler(ProjectError)
    async def project_error(request: Request, exc: ProjectError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.get("/agents", response_model=list[AgentInfo])
    async def list_agents(request: Request) -> list[AgentInfo]:
        st: AppState = request.app.state.cc
        owned = {a.runtime_name: a for a in st.store.list_agents()}
        return [
            AgentInfo(
                name=agent.name,
                tags=agent.tags,
                cwd=str(agent.cwd),
                session_id=st.sessions.get(agent.name),
                queue_depth=st.dispatcher.depth(agent.name),
                busy=st.workers[agent.name].busy,
                project=owned[agent.name].project_id if agent.name in owned else None,
                role=owned[agent.name].role if agent.name in owned else None,
            )
            for agent in st.registry.all_agents()
        ]

    @app.post("/messages", status_code=202, response_model=MessageAccepted)
    async def post_message(request: Request, body: MessageRequest) -> MessageAccepted:
        st: AppState = request.app.state.cc
        try:
            resolved = resolve_targets(body.tags, st.registry, st.sessions.as_dict())
        except DispatchError as exc:
            raise HTTPException(
                status_code=422,
                detail={"error": exc.message, "unmatched_tags": exc.unmatched},
            ) from exc

        message_id = uuid.uuid4().hex[:16]
        # One topic per message; per-agent fallback would scatter a fan-out
        # across files, so a fan-out with no topic lands under "global".
        raw_topic = body.topic or (
            resolved.agents[0] if len(resolved.agents) == 1 else "global"
        )
        try:
            topic = slugify(raw_topic)
        except LogStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        st.status.create(message_id, topic, body.tags, resolved.agents)
        for agent in resolved.agents:
            await st.dispatcher.enqueue(
                agent, Job(message_id=message_id, agent=agent, text=body.text, topic=topic)
            )
        log.info(
            "accepted message for %s", ", ".join(resolved.agents),
            extra={"message_id": message_id},
        )
        return MessageAccepted(message_id=message_id, targets=resolved.agents, topic=topic)

    @app.get("/messages/{message_id}", response_model=MessageStatusResponse)
    async def get_message(request: Request, message_id: str) -> MessageStatusResponse:
        record = request.app.state.cc.status.get(message_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown message_id")
        return MessageStatusResponse.of(record)

    @app.get("/logs")
    async def get_log_dates(request: Request) -> dict:
        return {"dates": request.app.state.cc.logstore.list_dates()}

    @app.get("/logs/{date}")
    async def get_log_topics(request: Request, date: str) -> dict:
        st: AppState = request.app.state.cc
        try:
            date = validate_date(date)
        except LogStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"date": date, "topics": st.logstore.list_topics(date)}

    @app.get("/logs/{date}/{topic}", response_class=PlainTextResponse)
    async def get_log(request: Request, date: str, topic: str) -> PlainTextResponse:
        st: AppState = request.app.state.cc
        try:
            content = st.logstore.read_topic(date, topic)
        except LogStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if content is None:
            raise HTTPException(status_code=404, detail="no log for that date and topic")
        return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")

    # -- projects (docs/PROJECTS.md) --------------------------------------- #

    @app.post("/projects", status_code=201, response_model=ProjectInfo)
    async def create_project(request: Request, body: ProjectCreate) -> ProjectInfo:
        svc: ProjectService = request.app.state.cc.projects
        project = svc.create_project(body)
        if body.manager is not None:
            manager = body.manager.model_copy(update={"role": "manager"})
            try:
                svc.add_agent(project, manager)
            except ProjectError:
                # A project whose manager could not be created is not the
                # project that was asked for; leave nothing half-built.
                await svc.delete_project(project)
                raise
        return svc.describe(project)

    @app.get("/projects", response_model=list[ProjectInfo])
    async def list_projects(request: Request) -> list[ProjectInfo]:
        svc: ProjectService = request.app.state.cc.projects
        return [svc.describe(p) for p in svc.store.list_projects()]

    @app.get("/projects/{pid}", response_model=ProjectInfo)
    async def get_project(request: Request, pid: str) -> ProjectInfo:
        svc: ProjectService = request.app.state.cc.projects
        return svc.describe(svc.require_project(pid))

    @app.delete("/projects/{pid}")
    async def delete_project(request: Request, pid: str) -> dict:
        svc: ProjectService = request.app.state.cc.projects
        project = svc.require_project(pid)
        await svc.delete_project(project)
        return {"deleted": pid}

    @app.get("/projects/{pid}/overview", response_class=PlainTextResponse)
    async def get_overview(request: Request, pid: str) -> PlainTextResponse:
        svc: ProjectService = request.app.state.cc.projects
        content = svc.read_overview(svc.require_project(pid))
        return PlainTextResponse(content, media_type="text/markdown; charset=utf-8")

    @app.put("/projects/{pid}/overview")
    async def put_overview(request: Request, pid: str) -> dict:
        # Read the raw body rather than declaring a Body(str): the brief is
        # Markdown, and FastAPI's JSON-first body handling would make the
        # request depend on the client setting a Content-Type.
        svc: ProjectService = request.app.state.cc.projects
        raw = await request.body()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=400, detail="body must be UTF-8") from exc
        path = svc.write_overview(svc.require_project(pid), content)
        return {"path": str(path), "bytes": len(raw)}

    @app.get("/projects/{pid}/agents", response_model=list[ProjectAgentRecord])
    async def list_project_agents(request: Request, pid: str) -> list[ProjectAgentRecord]:
        svc: ProjectService = request.app.state.cc.projects
        return svc.store.list_agents(svc.require_project(pid).id)

    @app.post("/projects/{pid}/agents", status_code=201, response_model=ProjectAgentRecord)
    async def add_project_agent(
        request: Request, pid: str, body: ProjectAgentCreate
    ) -> ProjectAgentRecord:
        svc: ProjectService = request.app.state.cc.projects
        return svc.add_agent(svc.require_project(pid), body)

    @app.delete("/projects/{pid}/agents/{name}")
    async def delete_project_agent(request: Request, pid: str, name: str) -> dict:
        svc: ProjectService = request.app.state.cc.projects
        cancelled = await svc.delete_agent(svc.require_project(pid), name)
        return {"deleted": name, "tasks_cancelled": cancelled}

    @app.get("/projects/{pid}/tasks", response_model=list[TaskRecord])
    async def list_project_tasks(
        request: Request, pid: str, status: str | None = None
    ) -> list[TaskRecord]:
        svc: ProjectService = request.app.state.cc.projects
        return svc.store.list_tasks(svc.require_project(pid).id, status=status)

    @app.post("/projects/{pid}/tasks", status_code=201, response_model=TaskRecord)
    async def create_task(request: Request, pid: str, body: TaskCreate) -> TaskRecord:
        svc: ProjectService = request.app.state.cc.projects
        return await svc.create_task(
            svc.require_project(pid), body.agent, body.title, body.text, created_by="api"
        )

    @app.post("/projects/{pid}/plan", response_model=PlanResult)
    async def plan_project(
        request: Request, pid: str, body: PlanRequest | None = None
    ) -> PlanResult:
        svc: ProjectService = request.app.state.cc.projects
        project = svc.require_project(pid)
        return await run_plan(svc, project, body.note if body else None)

    @app.get("/tasks", response_model=list[TaskRecord])
    async def list_tasks(
        request: Request,
        status: str | None = None,
        project: str | None = None,
        agent: str | None = None,
        limit: int = 200,
    ) -> list[TaskRecord]:
        st: AppState = request.app.state.cc
        return st.store.list_tasks(
            project_id=project, status=status, agent=agent, limit=min(limit, 1000)
        )

    @app.get("/tasks/{tid}", response_model=TaskRecord)
    async def get_task(request: Request, tid: str) -> TaskRecord:
        task = request.app.state.cc.store.get_task(tid)
        if task is None:
            raise HTTPException(status_code=404, detail="unknown task id")
        return task

    @app.post("/tasks/{tid}/cancel", response_model=TaskRecord)
    async def cancel_task(request: Request, tid: str) -> TaskRecord:
        return request.app.state.cc.projects.cancel_task(tid)

    # -- console ------------------------------------------------------------ #
    # Mounted last: a StaticFiles mount is greedy about its prefix, and the API
    # routes above must keep theirs.

    if config.web_dir.is_dir():
        @app.get("/", include_in_schema=False)
        async def console() -> RedirectResponse:
            return RedirectResponse("/web/")

        app.mount(
            "/web", StaticFiles(directory=config.web_dir, html=True), name="console"
        )
    else:
        log.warning("no console: %s is not a directory", config.web_dir)

    return app


configure_logging(os.environ.get("CC_AUTOMATION_LOG_LEVEL", "INFO"))
app = create_app()
