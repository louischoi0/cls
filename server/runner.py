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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .logstore import LogStore, LogStoreError
from .models import AgentConfig

log = logging.getLogger("cc_automation.runner")

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
    topic: str


@dataclass
class RunResult:
    ok: bool
    result_text: str
    session_id: str | None = None
    cost_usd: float | None = None
    duration_s: float = 0.0
    session_was_reset: bool = False


class SessionStore:
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
    claude_bin: str, agent: AgentConfig, text: str, session_id: str, resume: bool
) -> list[str]:
    """The exact command line for one invocation.

    Message text is passed as an argv element, never through a shell, so its
    content cannot become a command.
    """
    argv = [claude_bin, "-p", text, "--output-format", "json"]
    argv += ["--resume", session_id] if resume else ["--session-id", session_id]
    if agent.allowed_tools:
        argv += ["--allowedTools", ",".join(agent.allowed_tools)]
    argv += ["--permission-mode", agent.permission_mode]
    if agent.system_prompt:
        # Append rather than replace: --system-prompt would drop Claude Code's
        # own tool instructions and leave the agent unable to work.
        argv += ["--append-system-prompt", agent.system_prompt]
    argv += ["--max-budget-usd", str(agent.max_budget_usd)]
    return argv


class AgentWorker:
    def __init__(
        self,
        agent: AgentConfig,
        queue: asyncio.Queue,
        sessions: SessionStore,
        logstore: LogStore,
        status,
        claude_bin: str,
    ) -> None:
        self.agent = agent
        self.queue = queue
        self.sessions = sessions
        self.logstore = logstore
        self.status = status
        self.claude_bin = claude_bin
        self.busy = False

    async def run(self) -> None:
        while True:
            job = await self.queue.get()
            self.busy = True
            try:
                await self._process(job)
            except asyncio.CancelledError:
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
                self.busy = False
                self.queue.task_done()

    async def _process(self, job: Job) -> None:
        self.status.mark_running(job.message_id, self.agent.name)
        started = datetime.now()

        result = await self._invoke(job.text)

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
                env=scrubbed_env(),
                start_new_session=True,  # own process group, so we can kill the tree
            )
        except OSError as exc:
            return RunResult(False, f"could not start `claude`: {exc}")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.agent.timeout_s
            )
        except asyncio.TimeoutError:
            _kill_tree(proc)
            await proc.wait()
            return RunResult(
                False,
                f"timed out after {self.agent.timeout_s}s and was killed",
                duration_s=loop.time() - started,
            )

        duration = loop.time() - started
        out = stdout.decode("utf-8", "replace").strip()
        err = stderr.decode("utf-8", "replace").strip()

        if proc.returncode != 0:
            detail = err or out or "(no output)"
            return RunResult(
                False,
                f"`claude` exited {proc.returncode}: {detail}",
                duration_s=duration,
            )

        return _parse_result(out, err, duration)


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _parse_result(stdout: str, stderr: str, duration: float) -> RunResult:
    """Read the CLI's JSON result, tolerating a shape we did not expect."""
    if not stdout:
        return RunResult(False, f"`claude` produced no output. stderr: {stderr or '(empty)'}",
                         duration_s=duration)
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        return RunResult(False, f"could not parse `claude` JSON output:\n{stdout}",
                         duration_s=duration)

    if isinstance(data, list):
        data = next((d for d in reversed(data) if isinstance(d, dict)), None)
    if not isinstance(data, dict) or not data:
        return RunResult(False, f"unexpected `claude` JSON output:\n{stdout}",
                         duration_s=duration)

    # Only a JSON object carrying a string `result` counts as a real answer;
    # anything else is reported as a failure rather than passed off as output.
    text = data.get("result")
    has_result = isinstance(text, str)
    if not has_result:
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
