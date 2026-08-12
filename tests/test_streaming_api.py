"""Live output end to end: a real worker, a fake `claude` that streams, and the
SSE route the console reads.

The fake emits the shapes the installed CLI actually emits under
`--output-format stream-json`, so what is asserted here is the same parsing the
real thing goes through.
"""

import asyncio
import json
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.main import Config, create_app
from server.stream import StreamHub

KEY = "test-key-abcdefghijklmnop"
AUTH = {"X-API-Key": KEY}

#: One line per event, exactly as `claude --output-format stream-json` writes it.
STREAMED = [
    {"type": "system", "subtype": "init", "model": "claude-opus-5", "session_id": "sid-1"},
    {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}},
    {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/hosts"}}]}},
    {"type": "user", "message": {"content": [{"type": "tool_result", "is_error": False}]}},
    {"type": "assistant", "message": {"content": [{"type": "text", "text": "all done"}]}},
    {"type": "result", "is_error": False, "result": "all done", "session_id": "sid-1",
     "total_cost_usd": 0.02, "duration_ms": 1200},
]


def fake_claude(tmp_path: Path, lines, name: str = "fake-claude") -> str:
    """A `claude` that prints the given JSON objects, one per line.

    The payloads go in a sidecar file that the script `cat`s, rather than into
    the script itself: a JSON body full of `$` and quotes is a shell minefield,
    and `($0.05)` really does expand to the script's own path.
    """
    path = tmp_path / name
    data = tmp_path / f"{name}.jsonl"
    data.write_text("".join(json.dumps(p) + "\n" for p in lines), encoding="utf-8")
    path.write_text(f"#!/usr/bin/env bash\ncat {data}\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "work").mkdir()
    return tmp_path


def with_session(client, home: Path, name: str = "alpha"):
    """Every test here needs one session; it is created rather than declared."""
    response = client.post(
        "/sessions", json={"name": name, "cwd": str(home / "work"),
                           "allowed_tools": ["Read"]}, headers=AUTH,
    )
    assert response.status_code == 201, response.text
    return client


@pytest.fixture
def client(home: Path, tmp_path: Path):
    config = Config(
        home=home, api_key=KEY, start_workers=True,
        claude_bin=fake_claude(tmp_path, STREAMED),
    )
    with TestClient(create_app(config)) as c:
        yield with_session(c, home)


def wait_done(client, message_id: str) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline:
        body = client.get(f"/messages/{message_id}", headers=AUTH).json()
        if body["status"] in ("done", "failed"):
            return body
        time.sleep(0.02)
    raise AssertionError(f"message {message_id} stayed {body['status']}")


def sse_events(text: str) -> list[dict]:
    """The JSON payloads out of an SSE body."""
    out = []
    for frame in text.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: ") and line != "data: {}":
                out.append(json.loads(line[6:]))
    return out


def test_a_streamed_run_is_parsed_into_its_result(client):
    accepted = client.post(
        "/sessions/alpha/messages", json={"text": "ping"}, headers=AUTH
    ).json()
    record = wait_done(client, accepted["message_id"])
    assert record["status"] == "done"


def test_the_run_is_replayable_after_it_finishes(client):
    accepted = client.post(
        "/sessions/alpha/messages", json={"text": "ping"}, headers=AUTH
    ).json()
    wait_done(client, accepted["message_id"])

    resp = client.get(f"/messages/{accepted['message_id']}/stream", headers=AUTH)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = sse_events(resp.text)

    assert [e["kind"] for e in events] == [
        "start", "notice", "tool", "tool_result", "text", "result", "end",
    ]
    assert events[1]["text"] == "session started on claude-opus-5"
    assert events[2]["text"] == "Read /etc/hosts"
    assert events[4]["text"] == "all done"
    assert events[5]["text"] == "done · $0.0200"
    assert resp.text.rstrip().endswith("event: end\ndata: {}")
    # Every event is numbered, so a reader can tell a gap from a pause.
    assert [e["seq"] for e in events] == list(range(1, 8))


def test_a_run_nobody_kept_is_a_404(client):
    resp = client.get("/messages/never-ran/stream", headers=AUTH)
    assert resp.status_code == 404


def test_the_stream_needs_the_api_key(client):
    accepted = client.post(
        "/sessions/alpha/messages", json={"text": "ping"}, headers=AUTH
    ).json()
    wait_done(client, accepted["message_id"])
    assert client.get(f"/messages/{accepted['message_id']}/stream").status_code == 401


BUDGET_STOP = [
    {"type": "system", "subtype": "init", "model": "claude-opus-5"},
    {"type": "result", "is_error": True, "subtype": "error_max_budget_usd",
     "terminal_reason": "budget_exhausted",
     "errors": ["Reached maximum budget ($0.05)"], "total_cost_usd": 0.05},
]


def test_a_run_that_explains_itself_is_not_reported_as_a_bare_exit_code(
    tmp_path: Path, home: Path
):
    """The real shape of a budget stop: a `result` object on stdout, exit 1.

    Reporting "`claude` exited 1" here would throw away the one line that says
    what to do about it, and leave the operator with nothing to act on.
    """
    binary = fake_claude(tmp_path, BUDGET_STOP, name="fake-exit1")
    Path(binary).write_text(
        f"#!/usr/bin/env bash\ncat {tmp_path / 'fake-exit1.jsonl'}\nexit 1\n"
    )
    config = Config(home=home, api_key=KEY, start_workers=True, claude_bin=binary)
    with TestClient(create_app(config)) as client:
        with_session(client, home)
        accepted = client.post(
            "/sessions/alpha/messages", json={"text": "ping"}, headers=AUTH
        ).json()
        record = wait_done(client, accepted["message_id"])
        assert record["status"] == "failed"
        error = record["error"]
        assert error == "Reached maximum budget ($0.05)"
        assert "exited 1" not in error


def test_a_run_that_dies_without_a_result_reports_what_it_printed(
    tmp_path: Path, home: Path
):
    """No `result` object is the one case where the exit code is all there is —
    so whatever it did print has to come with it."""
    binary = tmp_path / "fake-noise"
    binary.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'Error: something went wrong before we started'\n"
        "exit 1\n"
    )
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    config = Config(home=home, api_key=KEY, start_workers=True, claude_bin=str(binary))
    with TestClient(create_app(config)) as client:
        with_session(client, home)
        accepted = client.post(
            "/sessions/alpha/messages", json={"text": "ping"}, headers=AUTH
        ).json()
        record = wait_done(client, accepted["message_id"])
        error = record["error"]
        assert "exited 1" in error
        assert "something went wrong before we started" in error
        assert "(no output)" not in error


def test_a_failing_run_still_streams_what_it_did(tmp_path: Path, home: Path):
    """A budget stop has no `result` text, but does say why — on the stream and
    in the record."""
    config = Config(
        home=home, api_key=KEY, start_workers=True,
        claude_bin=fake_claude(tmp_path, BUDGET_STOP, name="fake-budget"),
    )
    with TestClient(create_app(config)) as client:
        with_session(client, home)
        accepted = client.post(
            "/sessions/alpha/messages", json={"text": "ping"}, headers=AUTH
        ).json()
        record = wait_done(client, accepted["message_id"])
        assert record["status"] == "failed"
        assert "maximum budget" in record["error"]

        events = sse_events(
            client.get(f"/messages/{accepted['message_id']}/stream", headers=AUTH).text
        )
        assert events[-2]["kind"] == "result"
        assert events[-2]["text"] == "Reached maximum budget ($0.05)"


def test_an_over_long_line_is_dropped_without_losing_the_run(tmp_path: Path, home: Path):
    """One event lost beats a run lost: the `result` object is still read."""
    from server.runner import MAX_LINE

    binary = fake_claude(tmp_path, [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "x" * (MAX_LINE + 1000)}]}},
        {"type": "result", "is_error": False, "result": "survived"},
    ], name="fake-huge")

    config = Config(home=home, api_key=KEY, start_workers=True, claude_bin=binary)
    with TestClient(create_app(config)) as client:
        with_session(client, home)
        accepted = client.post(
            "/sessions/alpha/messages", json={"text": "ping"}, headers=AUTH
        ).json()
        assert wait_done(client, accepted["message_id"])["status"] == "done"


def test_the_hub_is_shared_by_every_worker(client):
    """One hub on the pool, not one per worker: a run is found by its id alone."""
    pool = client.app.state.cc.pool
    assert isinstance(pool.hub, StreamHub)
    assert all(w.hub is pool.hub for w in pool.workers.values())


def test_a_sealed_stream_carries_ciphertext_frames(client):
    """SSE under the sealed transport: the frames are envelopes, and the events
    inside them are the same ones the plaintext route yields."""
    from server.main import SEALED_MEDIA_TYPE  # noqa: F401
    from server.sealed import AUTH_HEADER, SEALED_HEADER, VERSION, SealedSession

    accepted = client.post(
        "/sessions/alpha/messages", json={"text": "ping"}, headers=AUTH
    ).json()
    wait_done(client, accepted["message_id"])

    session = SealedSession(KEY)
    path = f"/messages/{accepted['message_id']}/stream"
    auth, _ = session.seal_request("GET", path, None, None)
    resp = client.get(path, headers={SEALED_HEADER: VERSION, AUTH_HEADER: auth})

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Nothing the agent said survives in the clear.
    assert "all done" not in resp.text
    assert "session started" not in resp.text

    unsealed = "".join(
        session.open_event(line[6:])
        for frame in resp.text.split("\n\n") if frame.strip()
        for line in frame.splitlines() if line.startswith("data: ")
    )
    assert [e["kind"] for e in sse_events(unsealed)] == [
        "start", "notice", "tool", "tool_result", "text", "result", "end",
    ]
    assert "event: end" in unsealed
