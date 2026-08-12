"""FastAPI app: auth, the session routes, and the worker lifecycle.

The shape of the whole thing:

    Browser / client
        │  X-API-Key, or a sealed envelope (server/sealed.py)
        ▼
    API (this module)
        ├── SessionStore   — sessions and transcripts, one SQLite file
        ├── Registry       — the live sessions, by name
        ├── Dispatcher     — one FIFO queue per session
        ├── AgentPool      — one serial worker per session
        │       └── subprocess: claude -p --resume <session_id>
        ├── StreamHub      — a run's output, live, to whoever is watching
        ├── LogStore       — logs/{YYYY-MM-DD}/{session}.md
        └── Console        — static files at /web/

One session is one Claude Code conversation. Turns are serialised per session,
because two `claude --resume` processes on one session id would corrupt it.
"""

from __future__ import annotations

import asyncio
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
from fastapi.responses import (
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles


class ConsoleFiles(StaticFiles):
    """StaticFiles that always revalidates.

    The console is three files that reference each other's globals, so a
    browser holding a stale `render.js` beside a fresh `app.js` fails with
    "X is not defined" — the page half-loads and the cause is invisible.
    Without an explicit header a browser may reuse a cached copy *without
    asking*, so this pins revalidation on; the ETag still makes the usual
    answer a 304 with no body.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
        return response

from .dispatcher import Dispatcher
from .logstore import LogStore, LogStoreError
from .models import (
    ChatAccepted,
    ChatRequest,
    MessageRecord,
    SessionConfig,
    SessionCreate,
    SessionInfo,
    SessionUpdate,
    StatusStore,
    Turn,
)
from .pool import AgentPool
from .registry import Registry, RegistryError
from .runner import AgentWorker, Job, SessionIds, resolve_claude_bin
from .sealed import AUTH_HEADER, SEALED_HEADER, SealError, SealedSession
from .sessions import SessionStore, SessionStoreError

PROJECT_ROOT = Path(__file__).resolve().parent.parent

UNAUTHENTICATED_PATHS = frozenset({"/health", "/"})
# The console's own HTML/CSS/JS. A browser cannot put a header on a navigation,
# so the shell is served unauthenticated; it holds no secrets, and every call it
# makes carries the key the operator pasted into it.
UNAUTHENTICATED_PREFIXES = ("/web/",)

#: How long a quiet stream waits before sending a comment, so an idle proxy
#: does not decide the connection is dead.
HEARTBEAT_S = 15.0

#: Turns handed back by `GET /sessions/{name}/history` unless asked otherwise.
HISTORY_LIMIT = 200

log = logging.getLogger("cc_automation")


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key in ("message_id", "session"):
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
    logs_dir: Path | None = None
    state_dir: Path | None = None
    api_key: str | None = None
    api_key_file: Path = Path("~/.cc-automation/api_key")
    claude_bin: str | None = None
    start_workers: bool = True
    #: the console's static files; they ship with the code, not with `home`
    web_dir: Path | None = None
    #: the SQLite file holding sessions and their transcripts
    db_path: Path | None = None
    #: Refuse plaintext API calls, so the key cannot reach the wire by accident.
    #: Off by default: it breaks every `curl -H 'X-API-Key: ...'` in the docs,
    #: and on loopback there is nothing to protect against. Turn it on with the
    #: same breath as binding to 0.0.0.0 (`server/sealed.py`).
    require_sealed: bool = False

    def __post_init__(self) -> None:
        self.home = Path(self.home).expanduser()
        self.logs_dir = Path(self.logs_dir or self.home / "logs").expanduser()
        self.state_dir = Path(self.state_dir or self.home / "state").expanduser()
        self.api_key_file = Path(self.api_key_file).expanduser()
        self.web_dir = Path(self.web_dir or PROJECT_ROOT / "web").expanduser()
        self.db_path = Path(self.db_path or self.state_dir / "chat.db").expanduser()

    @classmethod
    def from_env(cls) -> "Config":
        env = os.environ.get
        return cls(
            home=Path(env("CC_AUTOMATION_HOME", str(PROJECT_ROOT))),
            logs_dir=Path(env("CC_AUTOMATION_LOGS_DIR")) if env("CC_AUTOMATION_LOGS_DIR") else None,
            state_dir=Path(env("CC_AUTOMATION_STATE_DIR")) if env("CC_AUTOMATION_STATE_DIR") else None,
            api_key=env("CC_AUTOMATION_API_KEY"),
            api_key_file=Path(env("CC_AUTOMATION_API_KEY_FILE", "~/.cc-automation/api_key")),
            claude_bin=env("CC_AUTOMATION_CLAUDE_BIN"),
            web_dir=Path(env("CC_AUTOMATION_WEB_DIR")) if env("CC_AUTOMATION_WEB_DIR") else None,
            db_path=Path(env("CC_AUTOMATION_DB")) if env("CC_AUTOMATION_DB") else None,
            require_sealed=env("CC_AUTOMATION_REQUIRE_SEALED", "").lower()
            in ("1", "true", "yes", "on"),
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


class Transcript:
    """The pool's observer: it writes each reply into the session's transcript.

    The worker already knows how a run went — this turns that into the `agent`
    turn the console replays after a reload. It is the observer rather than the
    route because a run outlives the request that queued it.
    """

    def __init__(self, store: SessionStore) -> None:
        self.store = store

    async def job_started(self, job: Job) -> bool:
        return True

    async def job_finished(self, job: Job, result) -> None:
        text = result.result_text or ("(no reply)" if result.ok else "run failed")
        if result.session_was_reset:
            text = "_(previous session could not be resumed; started a new one)_\n\n" + text
        self.store.add_turn(
            job.agent, "agent", text, message_id=job.message_id, failed=not result.ok
        )

    def worker_idle(self, agent: str) -> None:
        return None


@dataclass
class AppState:
    config: Config
    api_key: str
    registry: Registry
    dispatcher: Dispatcher
    session_ids: SessionIds
    logstore: LogStore
    status: StatusStore
    store: SessionStore
    #: The transport key derived from `api_key`, plus its replay guard.
    sealed: SealedSession = None  # type: ignore[assignment]
    pool: AgentPool = None  # type: ignore[assignment]

    @property
    def workers(self) -> dict[str, AgentWorker]:
        return self.pool.workers

    def info(self, config: SessionConfig, counts: dict) -> SessionInfo:
        turns, last_at = counts.get(config.name, (0, None))
        worker = self.workers.get(config.name)
        return SessionInfo(
            name=config.name,
            cwd=str(config.cwd),
            system_prompt=config.system_prompt,
            allowed_tools=config.allowed_tools,
            permission_mode=config.permission_mode,
            model=config.model,
            max_budget_usd=config.max_budget_usd,
            timeout_s=config.timeout_s,
            created_at=config.created_at,
            session_id=self.session_ids.get(config.name),
            queue_depth=self.dispatcher.depth(config.name),
            busy=bool(worker and worker.busy),
            turns=turns,
            last_at=last_at,
        )


#: Content type of a sealed body, in both directions.
SEALED_MEDIA_TYPE = "application/cc-sealed"
#: Carries the *inner* content type of a sealed response, so a client knows
#: whether it unsealed JSON or Markdown without having to guess from the bytes.
SEALED_TYPE_HEADER = "X-CC-Type"


def _full_path(request: Request) -> str:
    """Path and query as the claims sign it.

    The query string is half the request on routes like `?limit=50`, so it is
    signed too — otherwise a captured envelope could be re-pointed at a
    different slice.
    """
    query = request.url.query
    return f"{request.url.path}?{query}" if query else request.url.path


async def _unseal_request(session: SealedSession, request: Request) -> tuple[bytes, str | None]:
    auth = request.headers.get(AUTH_HEADER, "")
    if not auth:
        raise SealError("missing auth envelope")
    body = await request.body()
    return session.open_request(request.method, _full_path(request), auth, body)


def _replace_body(request: Request, plain: bytes, content_type: str | None) -> None:
    """Hand the routes the plaintext, as if it had arrived that way.

    Starlette's `BaseHTTPMiddleware` replays whatever `request.body()` cached to
    everything downstream, so overwriting that cache is all it takes — the
    routes below never learn the request was sealed, which is why sealing
    needed no changes to any of them.
    """
    request._body = plain
    headers = [
        (name, value)
        for name, value in request.scope["headers"]
        if name not in (b"content-length", b"content-type")
    ]
    headers.append((b"content-length", str(len(plain)).encode("latin-1")))
    if content_type:
        headers.append((b"content-type", content_type.encode("latin-1")))
    request.scope["headers"] = headers


def _passthrough_headers(response: Response) -> dict[str, str]:
    """Everything but the framing, which sealing rewrites."""
    return {
        name: value
        for name, value in response.headers.items()
        if name.lower() not in ("content-length", "content-type")
    }


async def _seal_response(session: SealedSession, response: Response) -> Response:
    inner_type = response.headers.get("content-type", "")
    if inner_type.startswith("text/event-stream"):
        return _seal_event_stream(session, response, inner_type)

    body = b"".join([chunk async for chunk in response.body_iterator])
    headers = _passthrough_headers(response)
    headers[SEALED_TYPE_HEADER] = inner_type
    return Response(
        content=session.seal_response(body),
        status_code=response.status_code,
        headers=headers,
        media_type=SEALED_MEDIA_TYPE,
    )


def _seal_event_stream(
    session: SealedSession, response: Response, inner_type: str
) -> StreamingResponse:
    """Seal an SSE feed frame by frame, because buffering it would defeat it.

    The transport stays SSE — one `data:` line per frame — but the line holds an
    envelope instead of the event JSON. Heartbeat comments pass through in the
    clear: they carry nothing, and a reader that cannot see them cannot tell a
    live connection from a stalled one.
    """

    async def frames():
        async for chunk in response.body_iterator:
            text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
            if text.startswith(":"):
                yield text
                continue
            yield f"data: {session.seal_event(text)}\n\n"

    headers = _passthrough_headers(response)
    headers[SEALED_TYPE_HEADER] = inner_type
    return StreamingResponse(
        frames(),
        status_code=response.status_code,
        headers=headers,
        media_type="text/event-stream",
    )


def create_app(
    config: Config | None = None,
    worker_factory: Callable[..., AgentWorker] = AgentWorker,
) -> FastAPI:
    config = config or Config.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        api_key = config.resolve_api_key()
        store = SessionStore(config.db_path)
        registry = Registry(store.list())
        state = AppState(
            config=config,
            api_key=api_key,
            sealed=SealedSession(api_key),
            registry=registry,
            dispatcher=Dispatcher(),
            session_ids=SessionIds(config.state_dir / "sessions.json"),
            logstore=LogStore(config.logs_dir),
            status=StatusStore(),
            store=store,
        )
        claude_bin = config.claude_bin or (
            resolve_claude_bin() if config.start_workers else "claude"
        )
        state.pool = AgentPool(
            registry=registry,
            dispatcher=state.dispatcher,
            sessions=state.session_ids,
            logstore=state.logstore,
            status=state.status,
            claude_bin=claude_bin,
            worker_factory=worker_factory,
            start_workers=config.start_workers,
            observer=Transcript(store),
        )
        # Sessions outlive the process; their workers do not. Everything the
        # store knows about gets one back on the way up.
        for session in registry.all_agents():
            state.pool.start(session)

        app.state.cc = state
        log.info(
            "started with %d session(s): %s (sealed: %s)",
            len(registry), ", ".join(registry.names) or "none",
            "required" if config.require_sealed else "optional",
        )
        try:
            yield
        finally:
            await state.pool.shutdown()
            state.store.close()

    app = FastAPI(title="cls chat console", version="2.0", lifespan=lifespan)

    @app.middleware("http")
    async def require_api_key(request: Request, call_next) -> Response:
        path = request.url.path
        if path in UNAUTHENTICATED_PATHS or path.startswith(UNAUTHENTICATED_PREFIXES):
            return await call_next(request)

        st: AppState = request.app.state.cc
        if request.headers.get(SEALED_HEADER):
            # A sealed request authenticates itself: only a holder of the API
            # key can produce a tag that verifies, so there is no key on the
            # wire to compare. `server/sealed.py` explains the trade against TLS.
            try:
                plain, content_type = await _unseal_request(st.sealed, request)
            except SealError:
                # One answer for every failure — bad tag, stale clock, replay,
                # mismatched claims. Distinguishing them would let a caller
                # probe the server with envelopes it cannot forge.
                return JSONResponse({"detail": "sealed request rejected"}, status_code=401)
            _replace_body(request, plain, content_type)
            response = await call_next(request)
            return await _seal_response(st.sealed, response)

        if st.config.require_sealed:
            return JSONResponse(
                {"detail": "this server accepts sealed requests only; see OPERATING.md"},
                status_code=426,
            )
        presented = request.headers.get("X-API-Key", "")
        expected = st.api_key
        if not hmac.compare_digest(presented, expected):
            return JSONResponse({"detail": "invalid or missing X-API-Key"}, status_code=401)
        return await call_next(request)

    @app.exception_handler(SessionStoreError)
    async def store_error(request: Request, exc: SessionStoreError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    # -- sessions -----------------------------------------------------------

    def _session_or_404(st: AppState, name: str) -> SessionConfig:
        config = st.registry.get(name)
        if config is None:
            raise HTTPException(status_code=404, detail=f"no session {name!r}")
        return config

    @app.get("/sessions", response_model=list[SessionInfo])
    async def list_sessions(request: Request) -> list[SessionInfo]:
        st: AppState = request.app.state.cc
        counts = st.store.counts()
        return [st.info(c, counts) for c in st.registry.all_agents()]

    @app.post("/sessions", response_model=SessionInfo, status_code=201)
    async def create_session(request: Request, body: SessionCreate) -> SessionInfo:
        st: AppState = request.app.state.cc
        config = body.to_config()
        if not config.cwd.is_dir():
            raise HTTPException(
                status_code=400, detail=f"cwd does not exist: {config.cwd}"
            )
        # The store decides whether the name is free — it is the record, and a
        # registry that disagreed with it would only be found on the next boot.
        st.store.create(config)
        try:
            st.pool.add(config)
        except RegistryError as exc:
            st.store.delete(config.name)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        log.info("created session %s", config.name, extra={"session": config.name})
        return st.info(config, st.store.counts())

    @app.get("/sessions/{name}", response_model=SessionInfo)
    async def get_session(request: Request, name: str) -> SessionInfo:
        st: AppState = request.app.state.cc
        return st.info(_session_or_404(st, name), st.store.counts())

    @app.patch("/sessions/{name}", response_model=SessionInfo)
    async def update_session(request: Request, name: str, body: SessionUpdate) -> SessionInfo:
        st: AppState = request.app.state.cc
        current = _session_or_404(st, name)
        changed = body.model_dump(exclude_none=True)
        updated = SessionConfig(**{**current.model_dump(), **changed})
        if not updated.cwd.is_dir():
            raise HTTPException(status_code=400, detail=f"cwd does not exist: {updated.cwd}")
        st.store.replace(updated)
        st.pool.reconfigure(updated)
        return st.info(updated, st.store.counts())

    @app.delete("/sessions/{name}", status_code=204)
    async def delete_session(request: Request, name: str) -> Response:
        st: AppState = request.app.state.cc
        _session_or_404(st, name)
        worker = st.workers.get(name)
        if worker is not None and worker.busy:
            # Cancelling mid-run would abandon the `claude` subprocess rather
            # than kill it, so the wait is deliberate; `timeout_s` is the
            # backstop that guarantees the wait ends.
            raise HTTPException(
                status_code=409, detail="session is running; wait for it to finish"
            )
        await st.pool.remove(name)
        st.store.delete(name)
        log.info("deleted session %s", name, extra={"session": name})
        return Response(status_code=204)

    # -- chat ---------------------------------------------------------------

    @app.get("/sessions/{name}/history", response_model=list[Turn])
    async def history(request: Request, name: str, limit: int = HISTORY_LIMIT) -> list[Turn]:
        st: AppState = request.app.state.cc
        _session_or_404(st, name)
        return st.store.history(name, limit=max(1, min(limit, 1000)))

    @app.delete("/sessions/{name}/history")
    async def clear_history(request: Request, name: str) -> dict:
        """Forget the transcript, keeping the session.

        The Claude Code conversation on the other side is untouched and still
        remembers — this clears what the console shows, not what the model
        knows. Deleting the session is how you start genuinely fresh.
        """
        st: AppState = request.app.state.cc
        _session_or_404(st, name)
        return {"removed": st.store.clear_history(name)}

    @app.post("/sessions/{name}/messages", response_model=ChatAccepted, status_code=202)
    async def say(request: Request, name: str, body: ChatRequest) -> ChatAccepted:
        """Queue one turn. The reply arrives on the stream and in the history."""
        st: AppState = request.app.state.cc
        _session_or_404(st, name)
        message_id = uuid.uuid4().hex[:16]
        st.store.add_turn(name, "user", body.text, message_id=message_id)
        st.status.create(message_id, name, body.text)
        await st.dispatcher.send(
            name, Job(message_id=message_id, agent=name, text=body.text, topic=name)
        )
        log.info(
            "queued a turn for %s", name,
            extra={"message_id": message_id, "session": name},
        )
        return ChatAccepted(message_id=message_id, session=name)

    @app.get("/messages/{message_id}", response_model=MessageRecord)
    async def message_status(request: Request, message_id: str) -> MessageRecord:
        st: AppState = request.app.state.cc
        record = st.status.get(message_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown message id")
        return record

    @app.get("/messages/{message_id}/stream")
    async def stream_message(request: Request, message_id: str) -> StreamingResponse:
        """Server-sent events: what this turn is doing, as it does it.

        History first, then the live feed, then `event: end`. A run that has
        already finished replays what it kept and ends, so opening a turn while
        it runs and opening it afterwards differ only in the scrollback.

        The API key rides on the request header like every other route, which is
        why the console reads this with `fetch` rather than `EventSource` —
        `EventSource` cannot set headers, and the alternative is a key in a URL.
        """
        hub = request.app.state.cc.pool.hub
        if hub.get(message_id) is None:
            raise HTTPException(
                status_code=404, detail="no live or recent output for that message"
            )

        async def events():
            queue: asyncio.Queue = asyncio.Queue()

            async def pump() -> None:
                try:
                    async for event in hub.subscribe(message_id):
                        await queue.put(event)
                finally:
                    await queue.put(None)

            task = asyncio.create_task(pump())
            try:
                while True:
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"  # a comment, so an idle proxy holds on
                        continue
                    if event is None:
                        yield "event: end\ndata: {}\n\n"
                        return
                    yield f"data: {json.dumps(event.as_dict())}\n\n"
            finally:
                # The reader went away; the run carries on without it.
                task.cancel()

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -- logs ---------------------------------------------------------------

    @app.get("/logs")
    async def list_log_dates(request: Request) -> list[str]:
        return request.app.state.cc.logstore.list_dates()

    @app.get("/logs/{date}")
    async def list_log_topics(request: Request, date: str) -> list[str]:
        try:
            return request.app.state.cc.logstore.list_topics(date)
        except LogStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/logs/{date}/{topic}", response_class=PlainTextResponse)
    async def read_log(request: Request, date: str, topic: str) -> str:
        try:
            text = request.app.state.cc.logstore.read_topic(date, topic)
        except LogStoreError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if text is None:
            raise HTTPException(status_code=404, detail="no log for that date and topic")
        return text

    # -- the console --------------------------------------------------------

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/web/")

    if config.web_dir.is_dir():
        app.mount("/web", ConsoleFiles(directory=config.web_dir, html=True), name="web")

    return app


configure_logging(os.environ.get("CC_AUTOMATION_LOG_LEVEL", "INFO"))
app_factory = create_app


def _build() -> FastAPI:
    return create_app()


app = _build()
