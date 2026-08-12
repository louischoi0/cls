"""Subprocess invocation of `claude -p`, one serial worker per agent.

Serialisation is the point: two `--resume` runs against the same session at the
same time would corrupt the conversation, so each agent gets exactly one worker
draining exactly one queue. Different agents still run in parallel.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import signal
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .logstore import LogStore, LogStoreError
from .models import SessionConfig
from .stream import StreamHub, describe

log = logging.getLogger("cc_automation.runner")

#: One stream-json line can carry a whole tool result. The asyncio default of
#: 64KiB would tear those in half; this is the cap past which a line is dropped
#: rather than buffered without limit.
MAX_LINE = 1 << 20
#: Enough stderr to explain a failure, not enough to hold a run's worth of noise.
MAX_STDERR_LINES = 200
#: Same, for lines on stdout that were not JSON at all.
MAX_NOISE_LINES = 20

# `claude` refuses to --resume an id it has never seen; that is recoverable.
_SESSION_GONE = re.compile(
    r"(no (conversation|session) found|session .* not found|could not resume)",
    re.IGNORECASE,
)


@dataclass
class Job:
    message_id: str
    agent: str
    text: str
    #: the log file this turn is written to; the session's own name
    topic: str
    #: resolved with the RunResult when a caller needs to await this one run
    future: "asyncio.Future | None" = field(default=None, repr=False, compare=False)


class JobObserver(Protocol):
    """Notified around one job. Failures here never fail the job."""

    async def job_started(self, job: Job) -> bool:
        """False drops the job before `claude` is spawned (a cancelled task)."""
        ...

    async def job_finished(self, job: Job, result: "RunResult") -> None: ...

    def worker_idle(self, agent: str) -> None:
        """The worker has let the job go and is free again.

        Distinct from `job_finished`, which runs while the job is still in
        flight: anything that reports *what an agent is doing* has to be told
        after the worker clears it, or it records the finished job forever.
        """
        ...


@dataclass
class RunResult:
    ok: bool
    result_text: str
    session_id: str | None = None
    cost_usd: float | None = None
    duration_s: float = 0.0
    session_was_reset: bool = False


class SessionIds:
    """agent name -> Claude Code session id, persisted across restarts."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path).expanduser()
        self._sessions: dict[str, str] = {}
        self.load()

    def load(self) -> None:
        if not self.path.is_file():
            self._sessions = {}
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("could not read %s (%s); starting with no sessions",
                        self.path, exc)
            self._sessions = {}
            return
        self._sessions = {
            str(k): str(v) for k, v in data.items() if isinstance(v, str)
        }

    def get(self, agent: str) -> str | None:
        return self._sessions.get(agent)

    def set(self, agent: str, session_id: str) -> None:
        if self._sessions.get(agent) == session_id:
            return
        self._sessions[agent] = session_id
        self._save()

    def as_dict(self) -> dict[str, str]:
        return dict(self._sessions)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._sessions, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)


def scrubbed_env() -> dict[str, str]:
    """Inherited env minus anything that marks us as being inside a session."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith("CLAUDE_CODE") and k != "CLAUDECODE"
    }


def resolve_claude_bin() -> str:
    explicit = os.environ.get("CC_AUTOMATION_CLAUDE_BIN")
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    raise RuntimeError("`claude` not found on PATH; set CC_AUTOMATION_CLAUDE_BIN")


def build_argv(
    claude_bin: str, agent: SessionConfig, text: str, session_id: str, resume: bool
) -> list[str]:
    """The exact command line for one invocation.

    Message text is passed as an argv element, never through a shell, so its
    content cannot become a command.
    """
    # stream-json emits one JSON object per line as the run happens, which is
    # what makes live output possible. Its final `result` object carries the
    # same fields the old single-shot `json` format did, so nothing downstream
    # of `_parse_result` changed. The CLI requires --verbose alongside it.
    argv = [claude_bin, "-p", text, "--output-format", "stream-json", "--verbose"]
    argv += ["--resume", session_id] if resume else ["--session-id", session_id]
    if agent.allowed_tools:
        argv += ["--allowedTools", ",".join(agent.allowed_tools)]
    argv += ["--permission-mode", agent.permission_mode]
    if agent.model:
        argv += ["--model", agent.model]
    if agent.system_prompt:
        # Append rather than replace: --system-prompt would drop Claude Code's
        # own tool instructions and leave the agent unable to work.
        argv += ["--append-system-prompt", agent.system_prompt]
    argv += ["--max-budget-usd", str(agent.max_budget_usd)]
    return argv


class AgentWorker:
    def __init__(
        self,
        agent: SessionConfig,
        queue: asyncio.Queue,
        sessions: SessionIds,
        logstore: LogStore,
        status,
        claude_bin: str,
        observer: JobObserver | None = None,
        env_provider: "Callable[[str], dict[str, str]] | None" = None,
        hub: StreamHub | None = None,
    ) -> None:
        self.agent = agent
        self.queue = queue
        self.sessions = sessions
        self.logstore = logstore
        self.status = status
        self.claude_bin = claude_bin
        self.observer = observer
        # Looked up per spawn rather than held on the config: credentials must
        # not live in something the API serialises back out.
        self.env_provider = env_provider
        self.hub = hub
        self.busy = False
        #: the job in flight, so callers can see *what* it is busy with
        self.current: Job | None = None
        #: non-JSON stdout from the run in flight, kept only to explain a failure
        self._noise: list[str] = []
        #: the run whose events are being published, keyed by message id
        self.stream_key: str | None = None

    async def run(self) -> None:
        while True:
            job = await self.queue.get()
            self.busy = True
            self.current = job
            try:
                await self._process(job)
            except asyncio.CancelledError:
                _settle(job, RunResult(False, "worker was cancelled"))
                raise
            except Exception:
                # A bug in our own bookkeeping must not take the worker down;
                # the next message still deserves to run.
                log.exception(
                    "worker %s failed handling message %s",
                    self.agent.name,
                    job.message_id,
                )
                self.status.mark_failed(job.message_id, self.agent.name, "internal error")
            finally:
                # A caller awaiting this job must never be left hanging, however
                # the job ended.
                _settle(job, RunResult(False, "job ended without a result"))
                self.busy = False
                self.current = None
                self.queue.task_done()
                self._worker_idle()

    async def _process(self, job: Job) -> None:
        self.status.mark_running(job.message_id, self.agent.name)
        if not await self._job_started(job):
            # Cancelled while it sat in the queue. Dropping it here is the only
            # cancellation that costs nothing; once `claude` is running,
            # timeout_s is the only stop.
            self.status.mark_failed(
                job.message_id, self.agent.name, "cancelled before it ran"
            )
            _settle(job, RunResult(False, "cancelled before it ran"))
            return
        started = datetime.now()

        # The run is keyed by message id: a task has one, and so does a plain
        # message, so both are watchable without a second identifier.
        if self.hub is not None:
            self.stream_key = job.message_id
            self.hub.open(job.message_id, self.agent.name)
        try:
            result = await self._invoke(job.text)
        finally:
            if self.hub is not None:
                self.hub.close(job.message_id)
                self.stream_key = None

        body = result.result_text
        if result.session_was_reset:
            body = (
                "_(previous session could not be resumed; started a new one)_\n\n"
                + body
            )

        try:
            await self.logstore.append_entry(
                date=started.strftime("%Y-%m-%d"),
                topic=job.topic,
                when=started,
                agent=self.agent.name,
                message_id=job.message_id,
                text=job.text,
                result=body,
                duration_s=result.duration_s,
                cost_usd=result.cost_usd,
                status="ok" if result.ok else "failed",
            )
        except LogStoreError:
            log.exception("could not write log entry for %s", job.message_id)

        if result.ok:
            self.status.mark_done(job.message_id, self.agent.name)
        else:
            self.status.mark_failed(job.message_id, self.agent.name, result.result_text)

        if self.observer is not None:
            try:
                await self.observer.job_finished(job, result)
            except Exception:
                log.exception("observer failed to record %s", job.message_id)
        _settle(job, result)

    def _worker_idle(self) -> None:
        if self.observer is None:
            return
        try:
            self.observer.worker_idle(self.agent.name)
        except Exception:
            log.exception("observer failed to record %s going idle", self.agent.name)

    async def _job_started(self, job: Job) -> bool:
        if self.observer is None:
            return True
        try:
            return await self.observer.job_started(job)
        except Exception:
            # An observer that cannot answer must not silently swallow the job.
            log.exception("observer failed to open %s", job.message_id)
            return True

    async def _invoke(self, text: str) -> RunResult:
        session_id = self.sessions.get(self.agent.name)
        resume = session_id is not None
        if session_id is None:
            session_id = str(uuid.uuid4())
            # Recorded before the run: if we are killed mid-invocation, the
            # session that Claude created is still ours to resume.
            self.sessions.set(self.agent.name, session_id)

        result = await self._spawn(text, session_id, resume)

        if not result.ok and resume and _SESSION_GONE.search(result.result_text):
            fresh = str(uuid.uuid4())
            log.warning(
                "agent %s: session %s is gone, starting %s",
                self.agent.name, session_id, fresh,
            )
            self.sessions.set(self.agent.name, fresh)
            result = await self._spawn(text, fresh, resume=False)
            result.session_was_reset = True

        if result.session_id:
            self.sessions.set(self.agent.name, result.session_id)
        return result

    def _env(self) -> dict[str, str]:
        env = scrubbed_env()
        if self.env_provider is not None:
            try:
                env.update(self.env_provider(self.agent.name))
            except Exception:
                # Missing credentials degrade the run; they do not stop it.
                log.exception("could not build the environment for %s", self.agent.name)
        return env

    async def _spawn(self, text: str, session_id: str, resume: bool) -> RunResult:
        argv = build_argv(self.claude_bin, self.agent, text, session_id, resume)
        loop = asyncio.get_running_loop()
        started = loop.time()

        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self.agent.cwd),
                env=self._env(),
                start_new_session=True,  # own process group, so we can kill the tree
                # One stream-json line carries a whole tool result, which the
                # default 64KiB would split into a LimitOverrunError.
                limit=MAX_LINE,
            )
        except OSError as exc:
            return RunResult(False, f"could not start `claude`: {exc}")

        errors: list[str] = []
        try:
            final, err = await asyncio.wait_for(
                asyncio.gather(self._read_stream(proc.stdout), _drain(proc.stderr, errors)),
                timeout=self.agent.timeout_s,
            )
            await proc.wait()
        except asyncio.TimeoutError:
            _kill_tree(proc)
            await proc.wait()
            return RunResult(
                False,
                f"timed out after {self.agent.timeout_s}s and was killed",
                duration_s=loop.time() - started,
            )

        duration = loop.time() - started
        err = "\n".join(errors).strip()

        # A non-zero exit with a `result` object is the CLI explaining itself —
        # a budget cap, a permission stop. Reporting "exited 1" instead would
        # throw that away and leave the operator with nothing to act on.
        if final is not None:
            return _parse_result(final, err, duration)

        if proc.returncode != 0:
            detail = err or "\n".join(self._noise) or "(no output)"
            return RunResult(
                False,
                f"`claude` exited {proc.returncode}: {detail}",
                duration_s=duration,
            )

        return _parse_result(final, err, duration)

    async def _read_stream(self, stdout) -> dict | None:
        """Consume the run line by line, publishing as it goes.

        Returns the last `result` object, which is the one that says how the run
        ended. Everything else is narration: useful to watch, not to keep.
        """
        final: dict | None = None
        self._noise = []
        while True:
            try:
                line = await stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                # A single line past MAX_LINE. Skipping it loses one event, not
                # the run; the `result` object is small and still to come.
                log.warning("agent %s: dropped an over-long output line", self.agent.name)
                continue
            if not line:
                return final
            raw = line.decode("utf-8", "replace").strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                # Not an event worth showing, but when a run dies without ever
                # reaching a `result` object, these lines are the only account
                # of why — so a few are kept for the failure message.
                log.debug("agent %s: non-JSON output line %.120r", self.agent.name, raw)
                if len(self._noise) < MAX_NOISE_LINES:
                    self._noise.append(raw)
                continue
            if not isinstance(payload, dict):
                continue
            if _is_final(payload):
                final = payload
            self._publish(payload)

    def _publish(self, payload: dict) -> None:
        if self.stream_key is None or self.hub is None:
            return
        try:
            described = describe(payload)
            if described is not None:
                self.hub.publish(self.stream_key, *described)
        except Exception:
            # Narration must never be able to fail a run.
            log.exception("could not publish a stream event for %s", self.agent.name)


def _settle(job: Job, result: RunResult) -> None:
    """Resolve a job's future once. Later calls are the safety net, not the answer."""
    if job.future is not None and not job.future.done():
        job.future.set_result(result)


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _is_final(payload: dict) -> bool:
    """Whether this object is the one that says how the run ended.

    `type: "result"` is what the CLI tags it with. An untagged object carrying
    `result` or `is_error` counts too, so a run that answers in the older
    single-object shape is still read rather than reported as no output.
    """
    kind = payload.get("type")
    if kind == "result":
        return True
    return kind is None and ("result" in payload or "is_error" in payload)


async def _drain(stream, into: list[str]) -> None:
    """Collect stderr so a failure can say why, without unbounded growth."""
    while True:
        try:
            line = await stream.readline()
        except (ValueError, asyncio.LimitOverrunError):
            continue
        if not line:
            return
        if len(into) < MAX_STDERR_LINES:
            into.append(line.decode("utf-8", "replace").rstrip())


def _parse_result(data: dict | None, stderr: str, duration: float) -> RunResult:
    """Read the run's final `result` object, tolerating a shape we did not expect."""
    if not data:
        return RunResult(False, f"`claude` produced no result. stderr: {stderr or '(empty)'}",
                         duration_s=duration)

    # Only a JSON object carrying a string `result` counts as a real answer;
    # anything else is reported as a failure rather than passed off as output.
    text = data.get("result")
    has_result = isinstance(text, str)
    if not has_result:
        # A budget or permission stop has no `result`, but does say why.
        errors = data.get("errors")
        if isinstance(errors, list) and errors:
            text = "; ".join(str(e) for e in errors)
        else:
            text = f"unexpected `claude` JSON output:\n{json.dumps(data, indent=2)}"

    cost = data.get("total_cost_usd")
    cost = float(cost) if isinstance(cost, (int, float)) else None

    reported = data.get("duration_ms")
    if isinstance(reported, (int, float)):
        duration = reported / 1000.0

    session_id = data.get("session_id")
    session_id = session_id if isinstance(session_id, str) else None

    is_error = bool(data.get("is_error"))
    return RunResult(
        ok=has_result and not is_error,
        result_text=text,
        session_id=session_id,
        cost_usd=cost,
        duration_s=duration,
    )
