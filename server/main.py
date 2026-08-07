"""FastAPI app: auth, routes, and the worker lifecycle."""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from .dispatcher import DispatchError, Dispatcher, resolve_targets
from .logstore import LogStore, LogStoreError, slugify, validate_date
from .models import (
    AgentInfo,
    MessageAccepted,
    MessageRequest,
    MessageStatusResponse,
    StatusStore,
)
from .registry import Registry, load_registry
from .runner import AgentWorker, Job, SessionStore, resolve_claude_bin

PROJECT_ROOT = Path(__file__).resolve().parent.parent
UNAUTHENTICATED_PATHS = frozenset({"/health"})

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

    def __post_init__(self) -> None:
        self.home = Path(self.home).expanduser()
        self.agents_file = Path(self.agents_file or self.home / "agents.yaml").expanduser()
        self.logs_dir = Path(self.logs_dir or self.home / "logs").expanduser()
        self.state_dir = Path(self.state_dir or self.home / "state").expanduser()
        self.api_key_file = Path(self.api_key_file).expanduser()

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
    workers: dict[str, AgentWorker] = field(default_factory=dict)
    tasks: list[asyncio.Task] = field(default_factory=list)


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
        )
        claude_bin = config.claude_bin or (
            resolve_claude_bin() if config.start_workers else "claude"
        )
        for agent in registry.all_agents():
            state.workers[agent.name] = worker_factory(
                agent=agent,
                queue=state.dispatcher.queue(agent.name),
                sessions=state.sessions,
                logstore=state.logstore,
                status=state.status,
                claude_bin=claude_bin,
            )
        if config.start_workers:
            state.tasks = [
                asyncio.create_task(w.run(), name=f"worker:{name}")
                for name, w in state.workers.items()
            ]
        app.state.cc = state
        log.info("started with agents: %s", ", ".join(registry.names))
        try:
            yield
        finally:
            for task in state.tasks:
                task.cancel()
            await asyncio.gather(*state.tasks, return_exceptions=True)

    app = FastAPI(title="Claude Code Automation Server", version="1.0", lifespan=lifespan)

    @app.middleware("http")
    async def require_api_key(request: Request, call_next) -> Response:
        if request.url.path in UNAUTHENTICATED_PATHS:
            return await call_next(request)
        presented = request.headers.get("X-API-Key", "")
        expected = request.app.state.cc.api_key
        if not hmac.compare_digest(presented, expected):
            return JSONResponse({"detail": "invalid or missing X-API-Key"}, status_code=401)
        return await call_next(request)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.get("/agents", response_model=list[AgentInfo])
    async def list_agents(request: Request) -> list[AgentInfo]:
        st: AppState = request.app.state.cc
        return [
            AgentInfo(
                name=agent.name,
                tags=agent.tags,
                cwd=str(agent.cwd),
                session_id=st.sessions.get(agent.name),
                queue_depth=st.dispatcher.depth(agent.name),
                busy=st.workers[agent.name].busy,
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

    return app


configure_logging(os.environ.get("CC_AUTOMATION_LOG_LEVEL", "INFO"))
app = create_app()
