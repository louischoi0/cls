"""The store: sessions and their transcripts.

Tested directly rather than only through the API, because it is the durable
record — a bug here outlives the process that made it.
"""

from pathlib import Path

import pytest

from server.models import SessionConfig
from server.sessions import SessionStore, SessionStoreError


@pytest.fixture
def store(tmp_path: Path) -> SessionStore:
    s = SessionStore(tmp_path / "chat.db")
    yield s
    s.close()


def make(name="alpha", cwd=Path("/tmp"), **kw) -> SessionConfig:
    return SessionConfig(name=name, cwd=cwd, **kw)


# -- sessions ---------------------------------------------------------------


def test_a_new_store_is_empty(store):
    assert store.list() == []
    assert store.get("alpha") is None


def test_a_session_round_trips(store):
    store.create(make("alpha", Path("/tmp/work"), allowed_tools=["Read", "Grep"],
                      model="claude-opus-5", timeout_s=60))
    found = store.get("alpha")
    assert found.name == "alpha"
    assert found.cwd == Path("/tmp/work")
    assert found.allowed_tools == ["Read", "Grep"]
    assert found.model == "claude-opus-5"
    assert found.timeout_s == 60


def test_an_empty_tool_list_stays_empty(store):
    """`"".split(",")` is `[""]`, which would invent a tool called nothing."""
    store.create(make("alpha", allowed_tools=[]))
    assert store.get("alpha").allowed_tools == []


def test_a_duplicate_name_is_refused(store):
    store.create(make("alpha"))
    with pytest.raises(SessionStoreError) as caught:
        store.create(make("alpha"))
    assert caught.value.status_code == 409


def test_sessions_come_back_in_creation_order(store):
    for name in ("gamma", "alpha", "beta"):
        store.create(make(name))
    assert [s.name for s in store.list()] == ["gamma", "alpha", "beta"]


def test_replacing_changes_settings(store):
    store.create(make("alpha", timeout_s=60))
    store.replace(make("alpha", timeout_s=300, allowed_tools=["Read"]))
    assert store.get("alpha").timeout_s == 300
    assert store.get("alpha").allowed_tools == ["Read"]


def test_replacing_something_absent_is_a_404(store):
    with pytest.raises(SessionStoreError) as caught:
        store.replace(make("ghost"))
    assert caught.value.status_code == 404


def test_deleting_removes_it(store):
    store.create(make("alpha"))
    store.delete("alpha")
    assert store.get("alpha") is None


def test_deleting_something_absent_is_a_404(store):
    with pytest.raises(SessionStoreError) as caught:
        store.delete("ghost")
    assert caught.value.status_code == 404


# -- transcripts ------------------------------------------------------------


def test_turns_come_back_oldest_first(store):
    store.create(make("alpha"))
    for word in ("one", "two", "three"):
        store.add_turn("alpha", "user", word)
    assert [t.text for t in store.history("alpha")] == ["one", "two", "three"]


def test_a_turn_keeps_what_it_was_given(store):
    store.create(make("alpha"))
    turn = store.add_turn("alpha", "agent", "hello", message_id="abc", failed=True)
    back = store.history("alpha")[0]
    assert (back.role, back.text, back.message_id, back.failed) == (
        "agent", "hello", "abc", True
    )
    assert back.id == turn.id


def test_the_limit_keeps_the_tail_not_the_head(store):
    """A chat window wants the end of the conversation, in reading order."""
    store.create(make("alpha"))
    for i in range(10):
        store.add_turn("alpha", "user", str(i))
    assert [t.text for t in store.history("alpha", limit=3)] == ["7", "8", "9"]


def test_transcripts_do_not_leak_between_sessions(store):
    store.create(make("alpha"))
    store.create(make("beta"))
    store.add_turn("alpha", "user", "for alpha")
    store.add_turn("beta", "user", "for beta")
    assert [t.text for t in store.history("alpha")] == ["for alpha"]
    assert [t.text for t in store.history("beta")] == ["for beta"]


def test_clearing_history_keeps_the_session(store):
    store.create(make("alpha"))
    store.add_turn("alpha", "user", "hi")
    assert store.clear_history("alpha") == 1
    assert store.history("alpha") == []
    assert store.get("alpha") is not None


def test_deleting_a_session_takes_its_turns_with_it(store):
    store.create(make("alpha"))
    store.add_turn("alpha", "user", "hi")
    store.delete("alpha")
    assert store.history("alpha") == []


def test_a_recreated_name_does_not_inherit_the_old_transcript(store):
    """The name is the identity, so deleting must not leave turns behind for
    the next session to adopt."""
    store.create(make("alpha"))
    store.add_turn("alpha", "user", "old life")
    store.delete("alpha")
    store.create(make("alpha"))
    assert store.history("alpha") == []


def test_counts_report_turns_and_the_latest(store):
    store.create(make("alpha"))
    store.add_turn("alpha", "user", "one")
    last = store.add_turn("alpha", "agent", "two")
    counts = store.counts()
    assert counts["alpha"][0] == 2
    assert counts["alpha"][1] == last.at


def test_a_session_with_no_turns_is_absent_from_counts(store):
    store.create(make("alpha"))
    assert "alpha" not in store.counts()


# -- durability -------------------------------------------------------------


def test_everything_survives_a_reopen(tmp_path: Path):
    path = tmp_path / "chat.db"
    first = SessionStore(path)
    first.create(make("alpha", Path("/tmp/work"), allowed_tools=["Read"]))
    first.add_turn("alpha", "user", "remember me")
    first.close()

    second = SessionStore(path)
    try:
        assert [s.name for s in second.list()] == ["alpha"]
        assert second.get("alpha").allowed_tools == ["Read"]
        assert [t.text for t in second.history("alpha")] == ["remember me"]
    finally:
        second.close()


def test_the_file_is_made_if_its_directory_is_not_there(tmp_path: Path):
    store = SessionStore(tmp_path / "deep" / "deeper" / "chat.db")
    try:
        assert (tmp_path / "deep" / "deeper" / "chat.db").exists()
    finally:
        store.close()
