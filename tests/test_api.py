"""The session and chat API.

Workers are not started, so a queued turn sits in its queue where a test can
look at it and no `claude` subprocess is ever spawned.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import AUTH, KEY
from server.main import Config, create_app
from server.sessions import SessionStore


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "work").mkdir()
    return tmp_path


@pytest.fixture
def workdir(home: Path) -> Path:
    return home / "work"


@pytest.fixture
def client(home: Path):
    config = Config(home=home, api_key=KEY, start_workers=False, claude_bin="claude")
    with TestClient(create_app(config)) as c:
        yield c


def new_session(client, name="alpha", cwd=None, **kw) -> dict:
    body = {"name": name, "cwd": str(cwd), **kw}
    response = client.post("/sessions", json=body, headers=AUTH)
    assert response.status_code == 201, response.text
    return response.json()


def queued(client, name: str) -> list:
    return list(client.app.state.cc.dispatcher.queue(name)._queue)


# -- auth -------------------------------------------------------------------


def test_health_needs_no_key(client):
    assert client.get("/health").status_code == 200
    assert client.get("/health").json() == {"status": "ok"}


def test_missing_key_is_rejected(client):
    assert client.get("/sessions").status_code == 401


def test_wrong_key_is_rejected(client):
    assert client.get("/sessions", headers={"X-API-Key": "nope"}).status_code == 401


# -- creating and deleting --------------------------------------------------


def test_there_are_no_sessions_to_begin_with(client):
    assert client.get("/sessions", headers=AUTH).json() == []


def test_a_session_can_be_created(client, workdir):
    body = new_session(client, "alpha", workdir, allowed_tools=["Read", "Grep"])
    assert body["name"] == "alpha"
    assert body["cwd"] == str(workdir)
    assert body["allowed_tools"] == ["Read", "Grep"]
    assert body["session_id"] is None      # minted on the first run
    assert body["turns"] == 0
    assert [s["name"] for s in client.get("/sessions", headers=AUTH).json()] == ["alpha"]


def test_a_created_session_gets_a_worker_and_a_queue(client, workdir):
    new_session(client, "alpha", workdir)
    state = client.app.state.cc
    assert "alpha" in state.workers
    assert "alpha" in state.dispatcher


def test_a_duplicate_name_is_refused(client, workdir):
    new_session(client, "alpha", workdir)
    again = client.post(
        "/sessions", json={"name": "alpha", "cwd": str(workdir)}, headers=AUTH
    )
    assert again.status_code == 409


def test_a_missing_cwd_is_refused(client, tmp_path):
    response = client.post(
        "/sessions", json={"name": "alpha", "cwd": str(tmp_path / "nope")}, headers=AUTH
    )
    assert response.status_code == 400
    assert "cwd" in response.json()["detail"]


def test_a_refused_session_leaves_nothing_behind(client, tmp_path):
    """A failed create must not leave a row the next boot would resurrect."""
    client.post(
        "/sessions", json={"name": "ghost", "cwd": str(tmp_path / "nope")}, headers=AUTH
    )
    assert client.get("/sessions", headers=AUTH).json() == []


@pytest.mark.parametrize("name", ["has space", "has/slash", "", "..", "a" * 65])
def test_an_unusable_name_is_refused(client, workdir, name):
    response = client.post(
        "/sessions", json={"name": name, "cwd": str(workdir)}, headers=AUTH
    )
    assert response.status_code == 422


def test_a_permission_mode_that_can_prompt_is_refused(client, workdir):
    """`claude -p` has nobody to ask, so a prompting mode hangs the worker."""
    response = client.post(
        "/sessions",
        json={"name": "alpha", "cwd": str(workdir), "permission_mode": "askAlways"},
        headers=AUTH,
    )
    assert response.status_code == 422


def test_a_session_can_be_deleted(client, workdir):
    new_session(client, "alpha", workdir)
    assert client.delete("/sessions/alpha", headers=AUTH).status_code == 204
    assert client.get("/sessions", headers=AUTH).json() == []
    state = client.app.state.cc
    assert "alpha" not in state.workers
    assert "alpha" not in state.dispatcher


def test_deleting_an_unknown_session_is_a_404(client):
    assert client.delete("/sessions/nope", headers=AUTH).status_code == 404


def test_deleting_a_busy_session_is_refused(client, workdir):
    """Cancelling mid-run would abandon the subprocess rather than kill it."""
    new_session(client, "alpha", workdir)
    client.app.state.cc.workers["alpha"].busy = True
    response = client.delete("/sessions/alpha", headers=AUTH)
    assert response.status_code == 409
    assert "running" in response.json()["detail"]


def test_a_session_can_be_reconfigured(client, workdir):
    new_session(client, "alpha", workdir, allowed_tools=["Read"])
    response = client.patch(
        "/sessions/alpha", json={"allowed_tools": ["Read", "Write"]}, headers=AUTH
    )
    assert response.status_code == 200
    assert response.json()["allowed_tools"] == ["Read", "Write"]
    # The worker takes the new settings for its next job.
    assert client.app.state.cc.workers["alpha"].agent.allowed_tools == ["Read", "Write"]


def test_reconfiguring_leaves_unmentioned_fields_alone(client, workdir):
    new_session(client, "alpha", workdir, model="claude-opus-5", timeout_s=60)
    body = client.patch("/sessions/alpha", json={"timeout_s": 120}, headers=AUTH).json()
    assert body["timeout_s"] == 120
    assert body["model"] == "claude-opus-5"


# -- chat -------------------------------------------------------------------


def test_a_turn_is_queued_and_recorded(client, workdir):
    new_session(client, "alpha", workdir)
    response = client.post(
        "/sessions/alpha/messages", json={"text": "hello there"}, headers=AUTH
    )
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["session"] == "alpha"

    assert [job.text for job in queued(client, "alpha")] == ["hello there"]
    history = client.get("/sessions/alpha/history", headers=AUTH).json()
    assert [(t["role"], t["text"]) for t in history] == [("user", "hello there")]
    assert history[0]["message_id"] == accepted["message_id"]


def test_talking_to_an_unknown_session_is_a_404(client):
    response = client.post("/sessions/nope/messages", json={"text": "hi"}, headers=AUTH)
    assert response.status_code == 404


def test_an_empty_turn_is_refused(client, workdir):
    new_session(client, "alpha", workdir)
    assert client.post(
        "/sessions/alpha/messages", json={"text": ""}, headers=AUTH
    ).status_code == 422


def test_history_is_oldest_first(client, workdir):
    new_session(client, "alpha", workdir)
    for word in ("one", "two", "three"):
        client.post("/sessions/alpha/messages", json={"text": word}, headers=AUTH)
    history = client.get("/sessions/alpha/history", headers=AUTH).json()
    assert [t["text"] for t in history] == ["one", "two", "three"]


def test_history_honours_its_limit_by_keeping_the_tail(client, workdir):
    new_session(client, "alpha", workdir)
    for i in range(5):
        client.post("/sessions/alpha/messages", json={"text": str(i)}, headers=AUTH)
    history = client.get("/sessions/alpha/history?limit=2", headers=AUTH).json()
    assert [t["text"] for t in history] == ["3", "4"]


def test_history_can_be_cleared_without_deleting_the_session(client, workdir):
    new_session(client, "alpha", workdir)
    client.post("/sessions/alpha/messages", json={"text": "hi"}, headers=AUTH)
    response = client.delete("/sessions/alpha/history", headers=AUTH)
    assert response.status_code == 200 and response.json()["removed"] == 1
    assert client.get("/sessions/alpha/history", headers=AUTH).json() == []
    assert client.get("/sessions/alpha", headers=AUTH).status_code == 200


def test_a_turn_count_is_reported(client, workdir):
    new_session(client, "alpha", workdir)
    client.post("/sessions/alpha/messages", json={"text": "hi"}, headers=AUTH)
    body = client.get("/sessions/alpha", headers=AUTH).json()
    assert body["turns"] == 1
    assert body["last_at"] is not None


def test_two_sessions_keep_separate_transcripts(client, workdir):
    new_session(client, "alpha", workdir)
    new_session(client, "beta", workdir)
    client.post("/sessions/alpha/messages", json={"text": "for alpha"}, headers=AUTH)
    client.post("/sessions/beta/messages", json={"text": "for beta"}, headers=AUTH)
    alpha = client.get("/sessions/alpha/history", headers=AUTH).json()
    beta = client.get("/sessions/beta/history", headers=AUTH).json()
    assert [t["text"] for t in alpha] == ["for alpha"]
    assert [t["text"] for t in beta] == ["for beta"]


def test_message_status_is_readable(client, workdir):
    new_session(client, "alpha", workdir)
    accepted = client.post(
        "/sessions/alpha/messages", json={"text": "hi"}, headers=AUTH
    ).json()
    body = client.get(f"/messages/{accepted['message_id']}", headers=AUTH).json()
    assert body["status"] == "queued"
    assert body["session"] == "alpha"


def test_an_unknown_message_id_is_a_404(client):
    assert client.get("/messages/nope", headers=AUTH).status_code == 404


# -- persistence ------------------------------------------------------------


def test_sessions_survive_a_restart(home, workdir):
    config = Config(home=home, api_key=KEY, start_workers=False, claude_bin="claude")
    with TestClient(create_app(config)) as c:
        new_session(c, "alpha", workdir)
        c.post("/sessions/alpha/messages", json={"text": "remember me"}, headers=AUTH)

    with TestClient(create_app(config)) as c:
        assert [s["name"] for s in c.get("/sessions", headers=AUTH).json()] == ["alpha"]
        history = c.get("/sessions/alpha/history", headers=AUTH).json()
        assert [t["text"] for t in history] == ["remember me"]
        # And it is live again, not merely listed.
        assert "alpha" in c.app.state.cc.workers


def test_deleting_a_session_drops_its_transcript(home, workdir):
    config = Config(home=home, api_key=KEY, start_workers=False, claude_bin="claude")
    with TestClient(create_app(config)) as c:
        new_session(c, "alpha", workdir)
        c.post("/sessions/alpha/messages", json={"text": "hi"}, headers=AUTH)
        c.delete("/sessions/alpha", headers=AUTH)

    store = SessionStore(config.db_path)
    try:
        assert store.history("alpha") == []
    finally:
        store.close()
