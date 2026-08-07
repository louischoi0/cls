"""API tests. Workers are not started, so accepted messages sit in their queues
and can be inspected directly — no `claude` subprocess is ever spawned."""

import asyncio
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.logstore import LogStore
from server.main import Config, create_app

KEY = "test-key-abcdefghijklmnop"
AUTH = {"X-API-Key": KEY}

AGENTS_YAML = """
agents:
  - name: alpha
    tags: [research, shared]
    cwd: {cwd}
    allowed_tools: [Read]
    permission_mode: bypassPermissions
  - name: beta
    tags: [ops, shared]
    cwd: {cwd}
    allowed_tools: [Read]
    permission_mode: bypassPermissions
"""


@pytest.fixture
def home(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "agents.yaml").write_text(AGENTS_YAML.format(cwd=work))
    return tmp_path


@pytest.fixture
def client(home: Path):
    config = Config(home=home, api_key=KEY, start_workers=False, claude_bin="claude")
    with TestClient(create_app(config)) as c:
        yield c


def queued(client, agent: str) -> list:
    queue = client.app.state.cc.dispatcher.queue(agent)
    return list(queue._queue)


def test_health_needs_no_key(client):
    assert client.get("/health").status_code == 200


def test_missing_key_is_rejected(client):
    assert client.get("/agents").status_code == 401


def test_wrong_key_is_rejected(client):
    assert client.get("/agents", headers={"X-API-Key": "nope"}).status_code == 401


def test_agents_lists_the_registry(client):
    body = client.get("/agents", headers=AUTH).json()
    assert [a["name"] for a in body] == ["alpha", "beta"]
    assert body[0]["session_id"] is None
    assert body[0]["queue_depth"] == 0


def test_global_reaches_every_agent(client):
    res = client.post(
        "/messages", headers=AUTH, json={"text": "hi", "tags": ["global"], "topic": "smoke"}
    )
    assert res.status_code == 202
    assert res.json()["targets"] == ["alpha", "beta"]
    assert len(queued(client, "alpha")) == 1 and len(queued(client, "beta")) == 1


def test_agent_tag_reaches_only_that_agent(client):
    res = client.post("/messages", headers=AUTH, json={"text": "hi", "tags": ["research"]})
    assert res.json()["targets"] == ["alpha"]
    assert queued(client, "beta") == []


def test_unknown_tag_is_422_not_a_silent_drop(client):
    res = client.post("/messages", headers=AUTH, json={"text": "hi", "tags": ["nonsense"]})
    assert res.status_code == 422
    assert res.json()["detail"]["unmatched_tags"] == ["nonsense"]
    assert queued(client, "alpha") == [] and queued(client, "beta") == []


def test_topic_defaults_to_agent_name_for_a_single_target(client):
    assert client.post(
        "/messages", headers=AUTH, json={"text": "hi", "tags": ["alpha"]}
    ).json()["topic"] == "alpha"


def test_topic_is_slugified(client):
    body = client.post(
        "/messages", headers=AUTH, json={"text": "hi", "tags": ["alpha"], "topic": "My Topic!"}
    ).json()
    assert body["topic"] == "my-topic"


def test_path_shaped_topic_is_rejected(client):
    res = client.post(
        "/messages",
        headers=AUTH,
        json={"text": "hi", "tags": ["alpha"], "topic": "../../etc/passwd"},
    )
    assert res.status_code == 400


def test_empty_text_and_empty_tags_are_rejected(client):
    assert client.post("/messages", headers=AUTH, json={"text": "", "tags": ["alpha"]}).status_code == 422
    assert client.post("/messages", headers=AUTH, json={"text": "hi", "tags": []}).status_code == 422


def test_message_status_starts_queued(client):
    mid = client.post(
        "/messages", headers=AUTH, json={"text": "hi", "tags": ["global"]}
    ).json()["message_id"]

    body = client.get(f"/messages/{mid}", headers=AUTH).json()
    assert body["status"] == "queued"
    assert [t["agent"] for t in body["targets"]] == ["alpha", "beta"]


def test_message_status_tracks_partial_progress(client):
    mid = client.post(
        "/messages", headers=AUTH, json={"text": "hi", "tags": ["global"]}
    ).json()["message_id"]
    status = client.app.state.cc.status

    status.mark_running(mid, "alpha")
    assert client.get(f"/messages/{mid}", headers=AUTH).json()["status"] == "running"

    status.mark_done(mid, "alpha")
    assert client.get(f"/messages/{mid}", headers=AUTH).json()["status"] == "running"

    status.mark_failed(mid, "beta", "boom")
    assert client.get(f"/messages/{mid}", headers=AUTH).json()["status"] == "failed"


def test_unknown_message_id_is_404(client):
    assert client.get("/messages/deadbeef", headers=AUTH).status_code == 404


def seed_log(client, date="2026-08-07", topic="smoke"):
    store: LogStore = client.app.state.cc.logstore
    asyncio.run(
        store.append_entry(
            date=date,
            topic=topic,
            when=datetime(2026, 8, 7, 9, 0, 0),
            agent="alpha",
            message_id="m1",
            text="hi",
            result="there",
            duration_s=1.0,
            cost_usd=0.01,
            status="ok",
        )
    )


def test_log_queries(client):
    seed_log(client)
    assert client.get("/logs", headers=AUTH).json()["dates"] == ["2026-08-07"]
    assert client.get("/logs/2026-08-07", headers=AUTH).json()["topics"] == ["smoke"]

    res = client.get("/logs/2026-08-07/smoke", headers=AUTH)
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/markdown")
    assert "**Input:** hi" in res.text


def test_missing_log_is_404(client):
    assert client.get("/logs/2026-08-07/nothing", headers=AUTH).status_code == 404


def test_bad_date_is_400(client):
    assert client.get("/logs/not-a-date", headers=AUTH).status_code == 400
    assert client.get("/logs/not-a-date/smoke", headers=AUTH).status_code == 400


@pytest.mark.parametrize(
    "path",
    [
        "/logs/2026-08-07/..%2f..%2fetc%2fpasswd",
        "/logs/2026-08-07/../../../etc/passwd",
        "/logs/..%2f..%2fetc/passwd",
        "/logs/2026-08-07/%2Fetc%2Fpasswd",
    ],
)
def test_traversal_attempts_never_read_outside_the_log_root(client, path):
    res = client.get(path, headers=AUTH)
    assert res.status_code in (400, 404)
    assert "root:" not in res.text
