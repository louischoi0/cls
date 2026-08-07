"""Runner tests. A fake `claude` script stands in for the real CLI, so the
suite never spends API credit and never needs the network."""

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from server.logstore import LogStore
from server.models import AgentConfig, StatusStore
from server.runner import (
    AgentWorker,
    Job,
    SessionStore,
    _parse_result,
    build_argv,
    scrubbed_env,
)


def fake_claude(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-claude"
    path.write_text("#!/usr/bin/env bash\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def agent(workdir):
    return AgentConfig(
        name="alpha",
        tags=[],
        cwd=workdir,
        system_prompt="be terse",
        allowed_tools=["Read", "Grep"],
        permission_mode="bypassPermissions",
        max_budget_usd=0.25,
        timeout_s=5,
    )


def test_first_run_creates_a_session(agent):
    argv = build_argv("claude", agent, "hi", "sess-1", resume=False)
    assert "--session-id" in argv and "--resume" not in argv
    assert argv[argv.index("--session-id") + 1] == "sess-1"


def test_later_runs_resume(agent):
    argv = build_argv("claude", agent, "hi", "sess-1", resume=True)
    assert "--resume" in argv and "--session-id" not in argv


def test_argv_carries_agent_config(agent):
    argv = build_argv("claude", agent, "hi", "s", resume=False)
    assert argv[:5] == ["claude", "-p", "hi", "--output-format", "json"]
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep"
    assert argv[argv.index("--permission-mode") + 1] == "bypassPermissions"
    assert argv[argv.index("--max-budget-usd") + 1] == "0.25"
    # Appended, not replaced: --system-prompt would strip Claude Code's own
    # tool instructions.
    assert argv[argv.index("--append-system-prompt") + 1] == "be terse"
    assert "--system-prompt" not in argv


def test_message_text_is_one_argv_element_not_a_shell_string(agent):
    argv = build_argv("claude", agent, "hi; rm -rf /", "s", resume=False)
    assert "hi; rm -rf /" in argv


def test_scrubbed_env_drops_session_markers(monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    monkeypatch.setenv("PATH_KEEPER", "keep")
    env = scrubbed_env()
    assert "CLAUDECODE" not in env and "CLAUDE_CODE_ENTRYPOINT" not in env
    assert env["PATH_KEEPER"] == "keep"


def test_parse_result_reads_the_cli_shape():
    payload = json.dumps(
        {
            "type": "result",
            "is_error": False,
            "result": "pong",
            "session_id": "abc",
            "total_cost_usd": 0.02,
            "duration_ms": 2500,
        }
    )
    res = _parse_result(payload, "", 99.0)
    assert (res.ok, res.result_text, res.session_id) == (True, "pong", "abc")
    assert res.cost_usd == 0.02
    assert res.duration_s == 2.5


def test_parse_result_flags_is_error():
    assert not _parse_result(json.dumps({"is_error": True, "result": "boom"}), "", 1).ok


@pytest.mark.parametrize("payload", ["", "not json", "[]", "3"])
def test_parse_result_survives_unexpected_output(payload):
    res = _parse_result(payload, "stderr detail", 1.0)
    assert res.ok is False and res.result_text


def build_worker(tmp_path, agent, claude_bin) -> tuple[AgentWorker, StatusStore, LogStore]:
    status = StatusStore()
    logstore = LogStore(tmp_path / "logs")
    worker = AgentWorker(
        agent=agent,
        queue=asyncio.Queue(),
        sessions=SessionStore(tmp_path / "sessions.json"),
        logstore=logstore,
        status=status,
        claude_bin=claude_bin,
    )
    return worker, status, logstore


def run_one(worker, status, job):
    status.create(job.message_id, job.topic, ["alpha"], ["alpha"])
    asyncio.run(worker._process(job))
    return status.get(job.message_id)


def test_successful_run_logs_and_persists_session(tmp_path, agent):
    binary = fake_claude(
        tmp_path,
        'echo \'{"is_error":false,"result":"pong","session_id":"sid-9","total_cost_usd":0.01}\'\n',
    )
    worker, status, _ = build_worker(tmp_path, agent, binary)
    record = run_one(worker, status, Job("m1", "alpha", "ping", "smoke"))

    assert record.status == "done"
    assert worker.sessions.get("alpha") == "sid-9"
    logged = next((tmp_path / "logs").rglob("smoke.md")).read_text()
    assert "**Input:** ping" in logged and "pong" in logged and "status=ok" in logged


def test_session_id_survives_a_store_reload(tmp_path, agent):
    binary = fake_claude(
        tmp_path, 'echo \'{"is_error":false,"result":"ok","session_id":"sid-keep"}\'\n'
    )
    worker, status, _ = build_worker(tmp_path, agent, binary)
    run_one(worker, status, Job("m1", "alpha", "ping", "smoke"))

    assert SessionStore(tmp_path / "sessions.json").get("alpha") == "sid-keep"


def test_nonzero_exit_is_recorded_as_failed(tmp_path, agent):
    binary = fake_claude(tmp_path, 'echo "kaboom" >&2\nexit 3\n')
    worker, status, _ = build_worker(tmp_path, agent, binary)
    record = run_one(worker, status, Job("m1", "alpha", "ping", "smoke"))

    assert record.status == "failed"
    logged = next((tmp_path / "logs").rglob("smoke.md")).read_text()
    assert "status=failed" in logged and "kaboom" in logged


def test_timeout_kills_and_fails(tmp_path, agent):
    agent = agent.model_copy(update={"timeout_s": 1})
    binary = fake_claude(tmp_path, "sleep 30\n")
    worker, status, _ = build_worker(tmp_path, agent, binary)
    record = run_one(worker, status, Job("m1", "alpha", "ping", "smoke"))

    assert record.status == "failed"
    assert "timed out" in record.target("alpha").error


def test_missing_binary_fails_without_killing_the_worker(tmp_path, agent):
    worker, status, _ = build_worker(tmp_path, agent, str(tmp_path / "does-not-exist"))
    record = run_one(worker, status, Job("m1", "alpha", "ping", "smoke"))
    assert record.status == "failed"


def test_dead_session_is_reset_and_retried(tmp_path, agent):
    # Fails on --resume, succeeds on --session-id: the recovery path.
    binary = fake_claude(
        tmp_path,
        'if [[ "$*" == *--resume* ]]; then\n'
        '  echo "No conversation found with session ID" >&2\n  exit 1\nfi\n'
        'echo \'{"is_error":false,"result":"fresh","session_id":"sid-new"}\'\n',
    )
    worker, status, _ = build_worker(tmp_path, agent, binary)
    worker.sessions.set("alpha", "sid-old")

    record = run_one(worker, status, Job("m1", "alpha", "ping", "smoke"))
    assert record.status == "done"
    assert worker.sessions.get("alpha") == "sid-new"
    logged = next((tmp_path / "logs").rglob("smoke.md")).read_text()
    assert "could not be resumed" in logged


def test_worker_loop_processes_messages_in_order(tmp_path, agent):
    binary = fake_claude(
        tmp_path,
        'sleep 0.2\necho "{\\"is_error\\":false,\\"result\\":\\"$3\\"}"\n',
    )
    worker, status, _ = build_worker(tmp_path, agent, binary)

    async def main():
        task = asyncio.create_task(worker.run())
        for i in range(3):
            job = Job(f"m{i}", "alpha", f"msg-{i}", "order")
            status.create(job.message_id, job.topic, ["alpha"], ["alpha"])
            await worker.queue.put(job)
        await worker.queue.join()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(main())

    logged = next((tmp_path / "logs").rglob("order.md")).read_text()
    assert [line for line in logged.splitlines() if line.startswith("**Input:**")] == [
        "**Input:** msg-0",
        "**Input:** msg-1",
        "**Input:** msg-2",
    ]
    assert all(status.get(f"m{i}").status == "done" for i in range(3))
