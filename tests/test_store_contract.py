"""One contract, both backends.

Every test here runs twice: once against SQLite, once against KDS. That is the
point — the KDS backend enforces in Python what SQLite enforces in the schema,
so "it behaves the same" has to be tested rather than asserted.

The KDS half is skipped unless a `kds_server` binary is findable:

    CC_AUTOMATION_KDS_BIN=/path/to/ckdbs/build/kds_server .venv/bin/python -m pytest
"""

import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from server.models import AgentConfig
from server.store import StoreError
from server.store.sqlite import SqliteProjectStore

BACKENDS = ["sqlite", "kds"]

# Text that the KDS wire cannot carry raw: an apostrophe has no escape in its
# string literals, and a comma or a newline would split a reply row.
HOSTILE = "it's got 'quotes', commas,\nnewlines, a ~tilde and a \\backslash"


def kds_binary() -> str | None:
    explicit = os.environ.get("CC_AUTOMATION_KDS_BIN")
    if explicit and Path(explicit).is_file():
        return explicit
    return shutil.which("kds_server")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def kds_server(tmp_path_factory):
    """One server for the session. There is no DROP TABLE, so tests share a
    schema and clear the rows between them."""
    binary = kds_binary()
    if binary is None:
        pytest.skip("no kds_server binary; set CC_AUTOMATION_KDS_BIN")

    data = tmp_path_factory.mktemp("kds") / "contract.db"
    port = free_port()
    proc = subprocess.Popen(
        [binary, str(data), "--port", str(port), "--log-file", ""],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    deadline = time.time() + 20
    while time.time() < deadline:
        if proc.poll() is not None:
            pytest.fail(f"kds_server exited with {proc.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.fail("kds_server did not start within 20s")

    yield port
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(params=BACKENDS)
def store(request, tmp_path: Path):
    if request.param == "sqlite":
        s = SqliteProjectStore(tmp_path / "projects.db")
        yield s
        s.close()
        return

    port = request.getfixturevalue("kds_server")
    from server.store.kds import KdsProjectStore

    s = KdsProjectStore("127.0.0.1", port)
    # No DROP TABLE and no TRUNCATE: an unqualified DELETE is how a shared
    # schema gets a clean table between tests.
    for table in ("tasks", "agents", "projects", "blobs"):
        s._conn.command(f"DELETE FROM {table}")
    yield s
    s.close()


def config(name: str, **kw) -> AgentConfig:
    return AgentConfig(name=name, cwd=Path("/tmp"), allowed_tools=["Read"], **kw)


def a_project(store, pid="demo", policy=("Read", "Edit")):
    return store.create_project(pid, f"Project {pid}", Path("/tmp"), list(policy))


# --- projects --------------------------------------------------------------- #


def test_project_round_trip(store):
    created = a_project(store)
    got = store.get_project("demo")
    assert got.id == created.id
    assert got.name == created.name
    assert got.root_dir == "/tmp"
    assert got.tool_policy == ["Read", "Edit"]
    assert got.created_at == created.created_at


def test_unknown_project_is_none(store):
    assert store.get_project("ghost") is None


def test_duplicate_project_is_refused(store):
    a_project(store)
    with pytest.raises(StoreError, match="already exists"):
        a_project(store)


def test_projects_list_in_id_order(store):
    for pid in ("charlie", "alpha", "bravo"):
        a_project(store, pid)
    assert [p.id for p in store.list_projects()] == ["alpha", "bravo", "charlie"]


def test_hostile_text_survives_a_project_name(store):
    store.create_project("demo", HOSTILE, Path("/tmp"), ["Read"])
    assert store.get_project("demo").name == HOSTILE


# --- agents ----------------------------------------------------------------- #


def test_agent_round_trip_including_its_config(store):
    a_project(store)
    cfg = config("demo__pm", system_prompt=HOSTILE, max_budget_usd=1.25, timeout_s=60)
    store.add_agent("demo", "pm", "manager", cfg)

    got = store.get_agent("demo", "pm")
    assert got.runtime_name == "demo__pm" and got.role == "manager"
    assert got.config.system_prompt == HOSTILE
    assert got.config.max_budget_usd == 1.25 and got.config.timeout_s == 60
    assert store.get_agent_by_runtime("demo__pm").name == "pm"
    assert store.get_agent_by_runtime("nope") is None


def test_one_projectmanager_per_project(store):
    a_project(store)
    store.add_agent("demo", "pm", "manager", config("demo__pm"))
    with pytest.raises(StoreError, match="already has a projectmanager"):
        store.add_agent("demo", "pm2", "manager", config("demo__pm2"))
    store.add_agent("demo", "w", "worker", config("demo__w"))  # a worker is fine


def test_managers_are_counted_per_project(store):
    for pid in ("a", "b"):
        a_project(store, pid)
        store.add_agent(pid, "pm", "manager", config(f"{pid}__pm"))
    assert len(store.list_agents()) == 2


def test_duplicate_agent_name_within_a_project_is_refused(store):
    a_project(store)
    store.add_agent("demo", "dev", "worker", config("demo__dev"))
    with pytest.raises(StoreError, match="already exists"):
        store.add_agent("demo", "dev", "worker", config("demo__dev2"))


def test_duplicate_runtime_name_is_refused(store):
    for pid in ("a", "b"):
        a_project(store, pid)
    store.add_agent("a", "dev", "worker", config("shared__name"))
    with pytest.raises(StoreError, match="taken"):
        store.add_agent("b", "dev", "worker", config("shared__name"))


def test_agents_list_scopes_to_a_project(store):
    for pid in ("a", "b"):
        a_project(store, pid)
        store.add_agent(pid, "dev", "worker", config(f"{pid}__dev"))
    assert [x.project_id for x in store.list_agents("a")] == ["a"]
    assert len(store.list_agents()) == 2


def test_deleting_an_agent_leaves_its_siblings(store):
    a_project(store)
    store.add_agent("demo", "pm", "manager", config("demo__pm"))
    store.add_agent("demo", "dev", "worker", config("demo__dev"))
    store.delete_agent("demo", "dev")
    assert [x.name for x in store.list_agents("demo")] == ["pm"]
    # the name is free again
    store.add_agent("demo", "dev", "worker", config("demo__dev"))


# --- tasks ------------------------------------------------------------------ #


def a_task(store, tid="t1", status=None, agent="demo__dev", project="demo"):
    task = store.create_task(tid, project, agent, f"Title {tid}", "do it", "api", f"m{tid}")
    if status:
        store.set_task_status(tid, status)
    return task


def test_task_round_trip(store):
    a_project(store)
    created = a_task(store)
    got = store.get_task("t1")
    assert got.status == "queued" and got.created_by == "api"
    assert got.message_id == "mt1" and got.text == "do it"
    assert got.created_at == created.created_at
    # everything unset is None, not an empty string
    assert got.result is None and got.error is None and got.cost_usd is None
    assert got.started_at is None and got.finished_at is None


def test_task_status_transitions_stamp_their_times(store):
    a_project(store)
    a_task(store)
    store.set_task_status("t1", "running")
    running = store.get_task("t1")
    assert running.status == "running" and running.started_at and not running.finished_at

    store.set_task_status("t1", "done", result="all good", cost_usd=0.0413)
    done = store.get_task("t1")
    assert done.status == "done" and done.finished_at
    assert done.result == "all good" and done.error is None
    assert done.cost_usd == pytest.approx(0.0413)


def test_failure_records_the_error_and_no_result(store):
    a_project(store)
    a_task(store)
    store.set_task_status("t1", "failed", error=HOSTILE)
    got = store.get_task("t1")
    assert got.status == "failed" and got.error == HOSTILE and got.result is None


def test_cancel_is_a_compare_and_set_on_queued(store):
    a_project(store)
    a_task(store)
    assert store.cancel_if_queued("t1") is True
    assert store.cancel_if_queued("t1") is False
    assert store.get_task("t1").status == "cancelled"

    a_task(store, "t2", status="running")
    assert store.cancel_if_queued("t2") is False
    assert store.get_task("t2").status == "running"
    assert store.cancel_if_queued("ghost") is False


def test_cancel_queued_for_agent_spares_the_others(store):
    a_project(store)
    a_task(store, "q1")
    a_task(store, "q2")
    a_task(store, "r1", status="running")
    a_task(store, "other", agent="demo__pm")

    assert store.cancel_queued_for_agent("demo__dev", "agent was deleted") == 2
    assert store.get_task("r1").status == "running"
    assert store.get_task("other").status == "queued"
    assert store.get_task("q1").error == "agent was deleted"


def test_fail_running_sweeps_only_running(store):
    a_project(store)
    a_task(store, "q1")
    a_task(store, "r1", status="running")
    a_task(store, "r2", status="running")

    assert store.fail_running("server restarted mid-run") == 2
    assert store.get_task("q1").status == "queued"
    assert store.get_task("r1").status == "failed"
    assert "restarted" in store.get_task("r1").error
    assert store.fail_running("again") == 0


def test_task_filters_and_ordering(store):
    for pid in ("a", "b"):
        a_project(store, pid)
    a_task(store, "t1", project="a", agent="a__dev")
    a_task(store, "t2", project="a", agent="a__pm", status="done")
    a_task(store, "t3", project="b", agent="b__dev")

    assert {t.id for t in store.list_tasks(project_id="a")} == {"t1", "t2"}
    assert [t.id for t in store.list_tasks(status="done")] == ["t2"]
    assert [t.id for t in store.list_tasks(agent="b__dev")] == ["t3"]
    assert len(store.list_tasks()) == 3
    # newest first, and the limit applies after the ordering
    assert [t.id for t in store.list_tasks()] == ["t3", "t2", "t1"]
    assert [t.id for t in store.list_tasks(limit=2)] == ["t3", "t2"]


# --- values ----------------------------------------------------------------- #


def test_hostile_text_survives_a_task(store):
    a_project(store)
    store.create_task("t1", "demo", "demo__dev", HOSTILE, HOSTILE, "api", "m1")
    store.set_task_status("t1", "done", result=HOSTILE)
    got = store.get_task("t1")
    assert got.title == HOSTILE and got.text == HOSTILE and got.result == HOSTILE


@pytest.mark.parametrize("size", [10, 8_000, 60_000])
def test_large_values_survive(store, size):
    """A KDS cell tops out at 8144 bytes, so anything longer is chunked."""
    a_project(store)
    body = "A" + ("x" * (size - 2)) + "Z"
    store.create_task("t1", "demo", "demo__dev", "T", body, "api", "m1")
    assert store.get_task("t1").text == body

    result = "R" + ("y" * (size - 2)) + "S"
    store.set_task_status("t1", "done", result=result)
    assert store.get_task("t1").result == result


def test_overwriting_a_large_value_keeps_the_new_one(store):
    a_project(store)
    a_task(store)
    store.set_task_status("t1", "done", result="z" * 40_000)
    store.set_task_status("t1", "done", result="short")
    assert store.get_task("t1").result == "short"


def test_empty_string_is_not_none(store):
    a_project(store)
    store.create_task("t1", "demo", "demo__dev", "", "", "api", "m1")
    got = store.get_task("t1")
    assert got.title == "" and got.text == ""
    assert got.result is None  # never set — distinct from ""


# --- cascade ---------------------------------------------------------------- #


def test_deleting_a_project_takes_its_agents_and_tasks(store):
    for pid in ("demo", "keep"):
        a_project(store, pid)
        store.add_agent(pid, "pm", "manager", config(f"{pid}__pm"))
        a_task(store, f"{pid}-task", project=pid, agent=f"{pid}__pm")
    store.set_task_status("demo-task", "done", result="w" * 30_000)

    store.delete_project("demo")

    assert store.get_project("demo") is None
    assert store.list_agents("demo") == []
    assert store.list_tasks(project_id="demo") == []
    # the other project is untouched
    assert store.get_project("keep") is not None
    assert len(store.list_agents("keep")) == 1
    assert len(store.list_tasks(project_id="keep")) == 1


def test_deleting_an_unknown_project_is_quiet(store):
    store.delete_project("ghost")


# --- backend selection ------------------------------------------------------ #


def test_the_deployed_default_is_kds_but_code_built_config_is_not():
    """Tests must never depend on an engine being up; the server should use it."""
    from server.main import DEFAULT_STORE_URL, Config

    assert DEFAULT_STORE_URL.startswith("kds://")
    assert "fallback=sqlite" in DEFAULT_STORE_URL

    saved = os.environ.pop("CC_AUTOMATION_STORE", None)
    try:
        assert Config.from_env().store_url == DEFAULT_STORE_URL
        assert Config(home="/tmp/nowhere").store_url.startswith("sqlite://")
    finally:
        if saved is not None:
            os.environ["CC_AUTOMATION_STORE"] = saved


def test_an_unreachable_engine_fails_loudly_without_a_fallback(tmp_path: Path):
    from server.store import open_store

    port = free_port()  # nothing is listening here
    with pytest.raises(StoreError, match="unreachable"):
        open_store(f"kds://127.0.0.1:{port}")


def test_the_declared_fallback_opens_sqlite_instead(tmp_path: Path):
    from server.store import open_store

    port = free_port()
    store = open_store(
        f"kds://127.0.0.1:{port}?fallback=sqlite", sqlite_path=tmp_path / "fb.db"
    )
    assert store.backend == "sqlite"
    store.create_project("p", "P", tmp_path, ["Read"])
    assert store.get_project("p") is not None
    store.close()


def test_a_bare_path_and_a_sqlite_url_name_the_same_file(tmp_path: Path):
    from server.store import open_store

    target = tmp_path / "explicit.db"
    for url in (str(target), f"sqlite://{target}"):
        store = open_store(url)
        assert store.backend == "sqlite"
        store.close()
    assert target.is_file()


def test_an_unknown_scheme_is_refused():
    from server.store import open_store

    with pytest.raises(StoreError, match="unsupported store URL scheme"):
        open_store("postgres://localhost/cls")
