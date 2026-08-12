"""The 1:1 correspondence between a console session and a CLI conversation.

The files here are written the way `claude` writes them, so what is asserted is
the reading of a real layout rather than of a shape invented for the test.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import AUTH, KEY
from server import clisessions
from server.main import Config, create_app
from server.runner import SessionIds, SessionIdTaken

SESSION_A = "66798295-26f4-45f9-9814-1896bd145559"
SESSION_B = "11111111-2222-3333-4444-555555555555"


def write_cli_session(root: Path, cwd: str, session_id: str, prompts=("hello",),
                      title: str | None = None) -> Path:
    """One conversation on disk, in the CLI's own directory layout and format."""
    project = root / clisessions.project_dir_name(cwd)
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    records = []
    for prompt in prompts:
        records.append({"type": "user", "cwd": cwd, "sessionId": session_id,
                        "message": {"role": "user", "content": prompt}})
        records.append({"type": "last-prompt", "lastPrompt": prompt,
                        "sessionId": session_id})
    if title:
        records.append({"type": "ai-title", "aiTitle": title, "sessionId": session_id})
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


@pytest.fixture
def cli_root(tmp_path: Path, monkeypatch) -> Path:
    root = tmp_path / "claude-projects"
    root.mkdir()
    monkeypatch.setenv("CC_AUTOMATION_CLI_SESSIONS_DIR", str(root))
    return root


# -- reading what is on disk ------------------------------------------------


def test_the_escape_is_the_cli_s_own():
    assert clisessions.project_dir_name("/home/cdkbs/workspaces/researcher") == (
        "-home-cdkbs-workspaces-researcher"
    )
    # A dot becomes a dash too, which is why `.claude` shows up as `--claude`.
    assert clisessions.project_dir_name("/home/x/.claude/worktrees/a") == (
        "-home-x--claude-worktrees-a"
    )


def test_a_conversation_is_read_off_disk(cli_root: Path):
    write_cli_session(cli_root, "/home/x/work", SESSION_A,
                      prompts=["first", "second"], title="Some title")
    found = clisessions.find(SESSION_A)
    assert found is not None
    assert found.session_id == SESSION_A
    assert found.cwd == "/home/x/work"          # from the records, not the name
    assert found.title == "Some title"
    assert found.last_prompt == "second"        # the latest, not the first
    assert found.size_bytes > 0


def test_the_cwd_comes_from_the_records_not_the_directory(cli_root: Path):
    """The escape is lossy, so the directory name is not a source of truth."""
    write_cli_session(cli_root, "/home/x/.config", SESSION_A)
    assert clisessions.find(SESSION_A).cwd == "/home/x/.config"


def test_scanning_finds_every_conversation_newest_first(cli_root: Path):
    import os
    import time

    a = write_cli_session(cli_root, "/home/x/one", SESSION_A)
    b = write_cli_session(cli_root, "/home/x/two", SESSION_B)
    os.utime(a, (time.time() - 600, time.time() - 600))
    assert [s.session_id for s in clisessions.scan()] == [SESSION_B, SESSION_A]


def test_scanning_reports_who_owns_each_one(cli_root: Path):
    write_cli_session(cli_root, "/home/x/work", SESSION_A)
    write_cli_session(cli_root, "/home/x/work", SESSION_B)
    found = {s.session_id: s.owner for s in clisessions.scan(owners={SESSION_A: "alpha"})}
    assert found[SESSION_A] == "alpha"
    assert found[SESSION_B] is None


def test_a_missing_root_is_empty_rather_than_an_error(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("CC_AUTOMATION_CLI_SESSIONS_DIR", str(tmp_path / "nope"))
    assert clisessions.scan() == []
    assert clisessions.find(SESSION_A) is None


def test_an_unreadable_line_does_not_lose_the_conversation(cli_root: Path):
    path = write_cli_session(cli_root, "/home/x/work", SESSION_A)
    path.write_text("not json\n" + path.read_text(), encoding="utf-8")
    assert clisessions.find(SESSION_A).cwd == "/home/x/work"


def test_a_huge_conversation_is_read_from_its_edges(cli_root: Path):
    """Only the ends are read, so a megabyte transcript is still one stat call
    away from a title — but the cwd is at the head and the prompt at the tail,
    which is exactly what the two edges cover."""
    path = write_cli_session(cli_root, "/home/x/work", SESSION_A, prompts=["first"])
    filler = json.dumps({"type": "assistant", "message": {"content": "x" * 500}}) + "\n"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(filler * 2000)                       # ~1 MB in the middle
        handle.write(json.dumps({"type": "last-prompt", "lastPrompt": "the last one",
                                 "sessionId": SESSION_A}) + "\n")
    found = clisessions.find(SESSION_A)
    assert found.cwd == "/home/x/work"          # head
    assert found.last_prompt == "the last one"  # tail
    assert found.size_bytes > 500_000


def test_the_path_of_a_conversation_that_has_not_run_yet(cli_root: Path):
    path = clisessions.path_for(SESSION_A, "/home/x/work")
    assert path == cli_root / "-home-x-work" / f"{SESSION_A}.jsonl"
    assert not path.is_file()


# -- the invariant ----------------------------------------------------------


def test_a_conversation_belongs_to_one_session(tmp_path: Path):
    ids = SessionIds(tmp_path / "sessions.json")
    ids.set("alpha", SESSION_A)
    with pytest.raises(SessionIdTaken):
        ids.set("beta", SESSION_A)
    assert ids.owner_of(SESSION_A) == "alpha"
    assert ids.get("beta") is None


def test_rebinding_the_same_pair_is_not_a_conflict(tmp_path: Path):
    ids = SessionIds(tmp_path / "sessions.json")
    ids.set("alpha", SESSION_A)
    ids.set("alpha", SESSION_A)          # idempotent
    assert ids.get("alpha") == SESSION_A


def test_a_session_may_be_repointed_at_another_conversation(tmp_path: Path):
    ids = SessionIds(tmp_path / "sessions.json")
    ids.set("alpha", SESSION_A)
    ids.set("alpha", SESSION_B)
    assert ids.get("alpha") == SESSION_B
    assert ids.owner_of(SESSION_A) is None     # freed by the move


def test_releasing_frees_the_conversation(tmp_path: Path):
    ids = SessionIds(tmp_path / "sessions.json")
    ids.set("alpha", SESSION_A)
    assert ids.release("alpha") == SESSION_A
    assert ids.owner_of(SESSION_A) is None
    ids.set("beta", SESSION_A)                 # now adoptable
    assert ids.owner_of(SESSION_A) == "beta"


def test_the_binding_survives_a_reload(tmp_path: Path):
    SessionIds(tmp_path / "sessions.json").set("alpha", SESSION_A)
    assert SessionIds(tmp_path / "sessions.json").owner_of(SESSION_A) == "alpha"


# -- through the API --------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path) -> Path:
    (tmp_path / "work").mkdir()
    return tmp_path


@pytest.fixture
def client(home: Path, cli_root: Path):
    config = Config(home=home, api_key=KEY, start_workers=False, claude_bin="claude")
    with TestClient(create_app(config)) as c:
        yield c


def test_the_cli_listing_is_served(client, cli_root: Path, home: Path):
    write_cli_session(cli_root, str(home / "work"), SESSION_A, title="A chat")
    body = client.get("/cli-sessions", headers=AUTH).json()
    assert [s["session_id"] for s in body] == [SESSION_A]
    assert body[0]["title"] == "A chat"
    assert body[0]["owner"] is None


def test_an_existing_conversation_can_be_adopted(client, cli_root: Path, home: Path):
    write_cli_session(cli_root, str(home / "work"), SESSION_A, title="A chat")
    made = client.post(
        "/sessions",
        json={"name": "alpha", "cwd": str(home / "work"), "session_id": SESSION_A},
        headers=AUTH,
    )
    assert made.status_code == 201, made.text
    assert made.json()["session_id"] == SESSION_A
    assert made.json()["cli_exists"] is True
    assert made.json()["cli_title"] == "A chat"
    # ...and the listing now says who owns it.
    listed = client.get("/cli-sessions", headers=AUTH).json()
    assert listed[0]["owner"] == "alpha"


def test_adopting_a_conversation_that_is_not_there_is_a_404(client, home: Path):
    response = client.post(
        "/sessions",
        json={"name": "alpha", "cwd": str(home / "work"), "session_id": SESSION_B},
        headers=AUTH,
    )
    assert response.status_code == 404
    assert client.get("/sessions", headers=AUTH).json() == []


def test_two_sessions_cannot_adopt_one_conversation(client, cli_root: Path, home: Path):
    write_cli_session(cli_root, str(home / "work"), SESSION_A)
    body = {"name": "alpha", "cwd": str(home / "work"), "session_id": SESSION_A}
    assert client.post("/sessions", json=body, headers=AUTH).status_code == 201
    clash = client.post(
        "/sessions", json={**body, "name": "beta"}, headers=AUTH
    )
    assert clash.status_code == 409
    assert "alpha" in clash.json()["detail"]
    # The rejected one left nothing behind.
    assert [s["name"] for s in client.get("/sessions", headers=AUTH).json()] == ["alpha"]


def test_deleting_a_session_frees_its_conversation(client, cli_root: Path, home: Path):
    write_cli_session(cli_root, str(home / "work"), SESSION_A)
    body = {"name": "alpha", "cwd": str(home / "work"), "session_id": SESSION_A}
    client.post("/sessions", json=body, headers=AUTH)
    client.delete("/sessions/alpha", headers=AUTH)

    # The transcript is the CLI's and stays on disk...
    assert clisessions.find(SESSION_A) is not None
    # ...and nothing owns it, so it can be adopted again.
    assert client.get("/cli-sessions", headers=AUTH).json()[0]["owner"] is None
    assert client.post(
        "/sessions", json={**body, "name": "beta"}, headers=AUTH
    ).status_code == 201


def test_a_session_with_no_conversation_yet_says_so(client, home: Path):
    made = client.post(
        "/sessions", json={"name": "alpha", "cwd": str(home / "work")}, headers=AUTH
    ).json()
    assert made["session_id"] is None
    assert made["cli_exists"] is False
    assert made["cli_path"] is None


# -- replaying a CLI transcript ---------------------------------------------


def write_conversation(root: Path, cwd: str, session_id: str, records: list) -> Path:
    project = root / clisessions.project_dir_name(cwd)
    project.mkdir(parents=True, exist_ok=True)
    path = project / f"{session_id}.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def convo(cwd="/home/x/work"):
    """The record shapes `claude` actually writes, in the order it writes them."""
    return [
        {"type": "user", "cwd": cwd, "timestamp": "2026-08-12T02:00:00.000Z",
         "message": {"role": "user", "content": "what is in this repo?"}},
        {"type": "assistant", "timestamp": "2026-08-12T02:00:01.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "thinking", "thinking": "hmm"},
             {"type": "tool_use", "name": "Read", "input": {"file_path": "/etc/hosts"}}]}},
        # A tool result comes back as a *user* record. It is not a user turn.
        {"type": "user", "timestamp": "2026-08-12T02:00:02.000Z",
         "message": {"role": "user", "content": [
             {"type": "tool_result", "tool_use_id": "t1",
              "content": "total 92\ndrwxrwxr-x 10 cdkbs cdkbs"}]}},
        {"type": "assistant", "timestamp": "2026-08-12T02:00:03.000Z",
         "message": {"role": "assistant", "content": [
             {"type": "text", "text": "A server and a console."}]}},
    ]


def test_a_transcript_reads_back_as_turns(cli_root: Path):
    path = write_conversation(cli_root, "/home/x/work", SESSION_A, convo())
    turns = clisessions.read_turns(path)
    assert [t.role for t in turns] == ["user", "agent"]
    assert turns[0].text == "what is in this repo?"
    assert turns[1].text == "A server and a console."


def test_a_tool_result_is_not_mistaken_for_something_a_person_said(cli_root: Path):
    """`user` names two different things in this format. Treating the second as
    a turn puts `total 92 drwxrwxr-x ...` on screen as if it were typed."""
    path = write_conversation(cli_root, "/home/x/work", SESSION_A, convo())
    turns = clisessions.read_turns(path)
    assert not any("drwx" in t.text for t in turns)
    assert len([t for t in turns if t.role == "user"]) == 1


def test_the_steps_under_a_reply_are_kept(cli_root: Path):
    path = write_conversation(cli_root, "/home/x/work", SESSION_A, convo())
    steps = clisessions.read_turns(path)[1].steps
    assert [s.kind for s in steps] == ["thinking", "tool", "tool_result"]
    assert steps[1].text == "Read(/etc/hosts)"


def test_a_turn_carries_the_time_it_happened(cli_root: Path):
    path = write_conversation(cli_root, "/home/x/work", SESSION_A, convo())
    assert clisessions.read_turns(path)[0].at.year == 2026


def test_the_limit_keeps_the_end_of_the_conversation(cli_root: Path):
    records = []
    for i in range(20):
        records.append({"type": "user", "cwd": "/home/x/work",
                        "message": {"role": "user", "content": f"q{i}"}})
        records.append({"type": "assistant",
                        "message": {"role": "assistant",
                                    "content": [{"type": "text", "text": f"a{i}"}]}})
    path = write_conversation(cli_root, "/home/x/work", SESSION_A, records)
    turns = clisessions.read_turns(path, limit=4)
    assert [t.text for t in turns] == ["q18", "a18", "q19", "a19"]


def test_an_empty_or_broken_transcript_is_no_turns(cli_root: Path):
    path = write_conversation(cli_root, "/home/x/work", SESSION_A, [])
    assert clisessions.read_turns(path) == []
    path.write_text("not json\n{}\n", encoding="utf-8")
    assert clisessions.read_turns(path) == []
    assert clisessions.read_turns(cli_root / "nope.jsonl") == []


def test_a_linked_session_replays_the_transcript(client, cli_root: Path, home: Path):
    """The whole point of the question: turns said in a terminal, before this
    console existed, show up in it."""
    write_conversation(cli_root, str(home / "work"), SESSION_A, convo(str(home / "work")))
    client.post("/sessions", json={"name": "alpha", "cwd": str(home / "work"),
                                   "session_id": SESSION_A}, headers=AUTH)
    body = client.get("/sessions/alpha/history", headers=AUTH).json()
    assert [t["text"] for t in body] == ["what is in this repo?", "A server and a console."]
    assert {t["source"] for t in body} == {"cli"}
    assert body[1]["steps"][1]["text"] == "Read(/etc/hosts)"


def test_the_console_s_own_record_can_still_be_asked_for(client, cli_root: Path, home: Path):
    write_conversation(cli_root, str(home / "work"), SESSION_A, convo(str(home / "work")))
    client.post("/sessions", json={"name": "alpha", "cwd": str(home / "work"),
                                   "session_id": SESSION_A}, headers=AUTH)
    client.post("/sessions/alpha/messages", json={"text": "queued here"}, headers=AUTH)

    console = client.get("/sessions/alpha/history?source=console", headers=AUTH).json()
    assert [t["text"] for t in console] == ["queued here"]
    assert {t["source"] for t in console} == {"console"}
    # ...and the transcript on disk does not know about it yet, because the run
    # has not happened.
    cli = client.get("/sessions/alpha/history?source=cli", headers=AUTH).json()
    assert "queued here" not in [t["text"] for t in cli]


def test_a_session_with_no_transcript_falls_back_to_the_console(client, home: Path):
    client.post("/sessions", json={"name": "alpha", "cwd": str(home / "work")},
                headers=AUTH)
    client.post("/sessions/alpha/messages", json={"text": "hello"}, headers=AUTH)
    body = client.get("/sessions/alpha/history", headers=AUTH).json()
    assert [t["text"] for t in body] == ["hello"]
    assert body[0]["source"] == "console"


def test_asking_for_cli_history_that_is_not_there_is_empty_not_a_guess(client, home: Path):
    client.post("/sessions", json={"name": "alpha", "cwd": str(home / "work")},
                headers=AUTH)
    assert client.get("/sessions/alpha/history?source=cli", headers=AUTH).json() == []


def test_a_transcript_is_found_after_its_session_changes_directory(cli_root: Path, home: Path):
    """`claude` files a conversation under the directory it is *running in*, so
    an agent that steps into a git worktree moves its own transcript. Deriving
    the path from the session's configured cwd finds nothing at that point —
    which is exactly what happened to a real session here."""
    ran_in = "/home/x/work/.claude/worktrees/feature"
    write_conversation(cli_root, ran_in, SESSION_A, convo(ran_in))

    # Where the configured cwd says it should be: nothing.
    assert not clisessions.path_for(SESSION_A, "/home/x/work").is_file()
    # Where it actually is, found by id alone.
    found = clisessions.locate(SESSION_A)
    assert found is not None and "worktrees-feature" in str(found)


def test_a_moved_transcript_still_replays(cli_root: Path, home: Path):
    config = Config(home=home, api_key=KEY, start_workers=False, claude_bin="claude")
    with TestClient(create_app(config)) as client:
        client.post("/sessions", json={"name": "alpha", "cwd": str(home / "work")},
                    headers=AUTH)
        # It ran, and ended up somewhere else entirely.
        elsewhere = str(home / "work" / ".claude" / "worktrees" / "feature")
        write_conversation(cli_root, elsewhere, SESSION_A, convo(elsewhere))
        client.app.state.cc.session_ids.set("alpha", SESSION_A)

        info = client.get("/sessions/alpha", headers=AUTH).json()
        assert info["cli_exists"] is True
        assert "worktrees-feature" in info["cli_path"]

        body = client.get("/sessions/alpha/history", headers=AUTH).json()
        assert [t["source"] for t in body] == ["cli", "cli"]


def test_reading_a_conversation_twice_does_not_read_the_file_twice(cli_root: Path):
    """The session list is polled every few seconds and every row wants a title."""
    path = write_cli_session(cli_root, "/home/x/work", SESSION_A, title="Cached")
    first = clisessions.read_session(path)
    path.chmod(0o000)          # unreadable now; only the cache can answer
    try:
        second = clisessions.read_session(path)
        assert second is not None and second.title == "Cached" == first.title
    finally:
        path.chmod(0o644)


def test_a_changed_conversation_is_re_read(cli_root: Path):
    path = write_cli_session(cli_root, "/home/x/work", SESSION_A, title="Before")
    assert clisessions.read_session(path).title == "Before"
    write_cli_session(cli_root, "/home/x/work", SESSION_A, title="After")
    assert clisessions.read_session(path).title == "After"
