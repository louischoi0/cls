"""Projects, project agents, tasks and planning rounds (docs/PROJECTS.md).

Workers are real `AgentWorker`s with `_spawn` replaced, so the observer hooks,
the log write, session bookkeeping and future settling are all exercised —
only the `claude` subprocess is faked.
"""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.main import Config, create_app
from server.planner import extract_json_object
from server.runner import AgentWorker, Job, RunResult

KEY = "test-key-abcdefghijklmnop"
AUTH = {"X-API-Key": KEY}

AGENTS_YAML = """
agents:
  - name: alpha
    tags: [research]
    cwd: {cwd}
    allowed_tools: [Read]
    permission_mode: bypassPermissions
"""


class FakeWorker(AgentWorker):
    """An AgentWorker whose subprocess is a scripted reply."""

    replies: dict[str, list[str]] = {}
    seen: list[tuple[str, str]] = []

    async def _spawn(self, text: str, session_id: str, resume: bool) -> RunResult:
        FakeWorker.seen.append((self.agent.name, text))
        queued = FakeWorker.replies.get(self.agent.name)
        reply = queued.pop(0) if queued else f"done: {text.splitlines()[0][:60]}"
        return RunResult(
            ok=True,
            result_text=reply,
            session_id=session_id,
            cost_usd=0.01,
            duration_s=0.1,
        )


@pytest.fixture(autouse=True)
def _reset_worker():
    FakeWorker.replies = {}
    FakeWorker.seen = []
    yield
    FakeWorker.replies = {}
    FakeWorker.seen = []


@pytest.fixture
def home(tmp_path: Path) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    (tmp_path / "agents.yaml").write_text(AGENTS_YAML.format(cwd=work))
    return tmp_path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """A project root with a brief and one subdirectory."""
    d = tmp_path / "proj"
    (d / "services" / "api").mkdir(parents=True)
    (d / "overview.md").write_text("# Demo\n\nShip the API.\n")
    return d


def make_client(home: Path, *, start_workers: bool = True) -> TestClient:
    config = Config(
        home=home, api_key=KEY, start_workers=start_workers, claude_bin="claude"
    )
    return TestClient(create_app(config, worker_factory=FakeWorker))


@pytest.fixture
def client(home: Path):
    with make_client(home) as c:
        yield c


def new_project(client, root: Path, **kw) -> dict:
    body = {
        "name": kw.pop("name", "Demo"),
        "root_dir": str(root),
        "manager": {
            "name": "pm",
            "role": "manager",
            "allowed_tools": ["Read", "Glob", "Grep"],
        },
        **kw,
    }
    resp = client.post("/projects", json=body, headers=AUTH)
    assert resp.status_code == 201, resp.text
    return resp.json()


def wait_for(client, task_id: str, *, terminal=("done", "failed", "cancelled")) -> dict:
    deadline = time.time() + 10
    while time.time() < deadline:
        task = client.get(f"/tasks/{task_id}", headers=AUTH).json()
        if task["status"] in terminal:
            return task
        time.sleep(0.02)
    raise AssertionError(f"task {task_id} stayed {task['status']}")


# --- projects and agents ---------------------------------------------------- #


def test_create_project_with_manager(client, root: Path):
    body = new_project(client, root)
    assert body["id"] == "demo"
    assert body["manager"] == "pm"
    assert body["tool_policy"] == ["Read", "Glob", "Grep", "Edit", "Write"]

    agents = client.get("/agents", headers=AUTH).json()
    by_name = {a["name"]: a for a in agents}
    assert by_name["demo__pm"]["project"] == "demo"
    assert by_name["demo__pm"]["role"] == "manager"
    assert by_name["alpha"]["project"] is None


def test_project_agent_gets_a_queue_without_a_restart(client, root: Path):
    new_project(client, root)
    resp = client.post(
        "/projects/demo/agents",
        json={"name": "dev", "allowed_tools": ["Read", "Edit"], "cwd": "services/api"},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["runtime_name"] == "demo__dev"
    dispatcher = client.app.state.cc.dispatcher
    assert dispatcher.depth("demo__dev") == 0  # exists, therefore no KeyError


def test_second_manager_is_refused(client, root: Path):
    new_project(client, root)
    resp = client.post(
        "/projects/demo/agents", json={"name": "pm2", "role": "manager"}, headers=AUTH
    )
    assert resp.status_code == 409
    assert "projectmanager" in resp.json()["detail"]


def test_tools_outside_the_policy_are_refused(client, root: Path):
    new_project(client, root)
    resp = client.post(
        "/projects/demo/agents",
        json={"name": "dev", "allowed_tools": ["Read", "Bash"]},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "Bash" in resp.json()["detail"]


def test_policy_matches_on_the_tool_base_name(client, root: Path, tmp_path: Path):
    new_project(
        client,
        root,
        name="Shell",
        tool_policy=["Read", "Bash"],
        manager={"name": "pm", "role": "manager", "allowed_tools": ["Read"]},
    )
    resp = client.post(
        "/projects/shell/agents",
        json={"name": "dev", "allowed_tools": ["Bash(ls *)"]},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text


def test_cwd_cannot_escape_the_project_root(client, root: Path):
    new_project(client, root)
    resp = client.post(
        "/projects/demo/agents", json={"name": "dev", "cwd": "../.."}, headers=AUTH
    )
    assert resp.status_code == 422
    assert "escapes" in resp.json()["detail"]


def test_empty_allowed_tools_falls_back_to_the_policy(client, root: Path):
    """An empty list would mean *every* tool to the CLI; it must not reach it."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    config = client.app.state.cc.registry.by_name["demo__dev"]
    assert config.allowed_tools == ["Read", "Glob", "Grep", "Edit", "Write"]


def test_agent_name_may_not_contain_the_runtime_separator(client, root: Path):
    new_project(client, root)
    resp = client.post("/projects/demo/agents", json={"name": "a__b"}, headers=AUTH)
    assert resp.status_code == 422


def test_deleting_an_agent_removes_it_from_the_registry(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    assert "demo__dev" in client.app.state.cc.registry.by_name
    resp = client.delete("/projects/demo/agents/dev", headers=AUTH)
    assert resp.status_code == 200
    assert "demo__dev" not in client.app.state.cc.registry.by_name


def test_deleting_a_project_removes_its_agents_and_rows(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    assert client.delete("/projects/demo", headers=AUTH).status_code == 200
    assert client.get("/projects/demo", headers=AUTH).status_code == 404
    assert "demo__pm" not in client.app.state.cc.registry.by_name
    assert client.app.state.cc.store.list_agents() == []


def test_deleting_a_project_with_a_running_task_is_refused(client, root: Path):
    new_project(client, root)
    store = client.app.state.cc.store
    store.create_task("trun", "demo", "demo__pm", "T", "x", "api", "mrun")
    store.set_task_status("trun", "running")
    resp = client.delete("/projects/demo", headers=AUTH)
    assert resp.status_code == 409
    assert client.get("/projects/demo", headers=AUTH).status_code == 200


def test_a_project_whose_manager_fails_is_not_half_built(client, root: Path):
    resp = client.post(
        "/projects",
        json={
            "name": "Broken",
            "root_dir": str(root),
            "manager": {"name": "pm", "role": "manager", "allowed_tools": ["Bash"]},
        },
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert client.get("/projects/broken", headers=AUTH).status_code == 404


def test_overview_round_trip(client, root: Path):
    new_project(client, root)
    assert "Ship the API" in client.get("/projects/demo/overview", headers=AUTH).text
    client.put("/projects/demo/overview", content="# New brief\n", headers=AUTH)
    assert (root / "overview.md").read_text() == "# New brief\n"


def test_project_agents_are_routable_by_project_tag(client, root: Path):
    new_project(client, root)
    resp = client.post(
        "/messages", json={"text": "hi", "tags": ["project:demo"]}, headers=AUTH
    )
    assert resp.status_code == 202
    assert resp.json()["targets"] == ["demo__pm"]


def test_yaml_agents_may_not_claim_a_project_tag(home: Path, tmp_path: Path):
    (home / "agents.yaml").write_text(
        AGENTS_YAML.format(cwd=tmp_path / "work") + "    tags: [project:demo]\n"
    )
    with pytest.raises(Exception, match="reserved"):
        with make_client(home):
            pass


# --- tasks ------------------------------------------------------------------ #


def test_task_runs_and_records_its_result(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    FakeWorker.replies["demo__dev"] = ["wrote the handler"]

    resp = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "Add handler", "text": "add POST /ping"},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    task = wait_for(client, resp.json()["id"])

    assert task["status"] == "done"
    assert task["result"] == "wrote the handler"
    assert task["cost_usd"] == 0.01
    assert task["started_at"] and task["finished_at"]
    # the instruction the worker actually received carries title and text
    _, text = FakeWorker.seen[-1]
    assert "Add handler" in text and "add POST /ping" in text


def test_task_writes_a_log_entry_under_the_project_topic(client, root: Path, home: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    resp = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "T", "text": "do it"},
        headers=AUTH,
    )
    wait_for(client, resp.json()["id"])

    dates = client.get("/logs", headers=AUTH).json()["dates"]
    log = client.get(f"/logs/{dates[-1]}/demo", headers=AUTH).text
    assert "agent: demo__dev" in log and "do it" in log


def test_failed_run_lands_on_the_task(client, root: Path, monkeypatch):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)

    async def broken(self, text, session_id, resume):
        return RunResult(False, "`claude` exited 1: boom")

    monkeypatch.setattr(FakeWorker, "_spawn", broken)
    resp = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "T", "text": "do it"},
        headers=AUTH,
    )
    task = wait_for(client, resp.json()["id"])
    assert task["status"] == "failed"
    assert "boom" in task["error"]
    assert task["result"] is None


def test_task_for_an_unknown_agent_is_404(client, root: Path):
    new_project(client, root)
    resp = client.post(
        "/projects/demo/tasks",
        json={"agent": "ghost", "title": "T", "text": "x"},
        headers=AUTH,
    )
    assert resp.status_code == 404


def test_observer_vetoes_a_cancelled_task(client, root: Path):
    """The veto in job_started is the only cancellation that costs nothing."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    store = client.app.state.cc.store
    svc = client.app.state.cc.projects
    store.create_task("tzz", "demo", "demo__dev", "T", "do it", "api", "mzz")
    job = Job(message_id="mzz", agent="demo__dev", text="x", topic="demo", task_id="tzz")

    assert client.portal.call(svc.job_started, job) is True
    assert store.get_task("tzz").status == "running"

    store.set_task_status("tzz", "cancelled")
    assert client.portal.call(svc.job_started, job) is False
    # a job with no task behind it is never vetoed
    assert client.portal.call(svc.job_started, Job("m", "demo__dev", "x", "demo")) is True


def test_a_cancelled_job_never_reaches_claude(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    store = client.app.state.cc.store
    queue = client.app.state.cc.dispatcher.queue("demo__dev")

    store.create_task("tzz", "demo", "demo__dev", "Dropped", "x", "api", "mzz")
    store.cancel_if_queued("tzz")
    client.portal.call(
        _put,
        queue,
        Job(message_id="mzz", agent="demo__dev", text="dropped", topic="demo", task_id="tzz"),
    )
    # A live task behind it: once this one has run, the queue is drained past
    # the cancelled one, so "never ran" is a fact rather than a race.
    live = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "Live", "text": "kept"},
        headers=AUTH,
    ).json()["id"]
    assert wait_for(client, live)["status"] == "done"

    assert [text for _, text in FakeWorker.seen] == [f"[task {live}] Live\n\nkept"]
    assert store.get_task("tzz").status == "cancelled"


async def _put(queue, job) -> None:
    queue.put_nowait(job)


def test_cancel_endpoint_refuses_a_finished_task(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    resp = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "T", "text": "do it"},
        headers=AUTH,
    )
    tid = resp.json()["id"]
    wait_for(client, tid)
    assert client.post(f"/tasks/{tid}/cancel", headers=AUTH).status_code == 409


def test_deleting_an_agent_with_a_running_task_is_refused(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    store = client.app.state.cc.store
    store.create_task("trun", "demo", "demo__dev", "T", "x", "api", "mrun")
    store.set_task_status("trun", "running")
    resp = client.delete("/projects/demo/agents/dev", headers=AUTH)
    assert resp.status_code == 409
    assert "running task" in resp.json()["detail"]


def test_tasks_are_queryable_across_projects(client, root: Path, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    new_project(client, root)
    new_project(client, other, name="Other")
    for pid in ("demo", "other"):
        client.post(f"/projects/{pid}/agents", json={"name": "dev"}, headers=AUTH)
        resp = client.post(
            f"/projects/{pid}/tasks",
            json={"agent": "dev", "title": "T", "text": "x"},
            headers=AUTH,
        )
        wait_for(client, resp.json()["id"])

    done = client.get("/tasks?status=done", headers=AUTH).json()
    assert {t["project_id"] for t in done} == {"demo", "other"}
    assert len(client.get("/tasks?project=demo", headers=AUTH).json()) == 1


# --- restart ---------------------------------------------------------------- #


def test_queued_tasks_survive_a_restart_and_running_ones_fail(home: Path, root: Path):
    with make_client(home, start_workers=False) as c:
        new_project(c, root)
        c.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
        queued = c.post(
            "/projects/demo/tasks",
            json={"agent": "dev", "title": "Queued", "text": "run me later"},
            headers=AUTH,
        ).json()["id"]
        store = c.app.state.cc.store
        store.create_task("tmid", "demo", "demo__dev", "Mid", "x", "api", "mmid")
        store.set_task_status("tmid", "running")

    with make_client(home) as c:
        assert wait_for(c, queued)["status"] == "done"
        interrupted = c.get("/tasks/tmid", headers=AUTH).json()
        assert interrupted["status"] == "failed"
        assert "restarted" in interrupted["error"]
        # the agent is back in the registry without touching agents.yaml
        assert "demo__dev" in c.app.state.cc.registry.by_name


# --- planning --------------------------------------------------------------- #


def test_extract_json_object_tolerates_fences_and_prose():
    plan = {"summary": "s", "actions": []}
    body = json.dumps(plan)
    assert extract_json_object(body) == plan
    assert extract_json_object(f"Here is the plan:\n```json\n{body}\n```\n") == plan
    assert extract_json_object(f"note {{not json}} then {body}") == plan
    assert extract_json_object('{"a": "} not the end"}') == {"a": "} not the end"}
    with pytest.raises(ValueError):
        extract_json_object("no object here")


def test_plan_creates_an_agent_and_a_task(client, root: Path):
    new_project(client, root)
    FakeWorker.replies["demo__pm"] = [
        "```json\n"
        + json.dumps(
            {
                "summary": "start the API",
                "actions": [
                    {
                        "op": "create_agent",
                        "name": "api-dev",
                        "system_prompt": "build the API",
                        "allowed_tools": ["Read", "Edit", "Write"],
                        "cwd": "services/api",
                    },
                    {
                        "op": "create_task",
                        "agent": "api-dev",
                        "title": "Scaffold",
                        "text": "create the app module",
                    },
                    {"op": "note", "text": "will revisit tests later"},
                ],
            }
        )
        + "\n```"
    ]

    resp = client.post("/projects/demo/plan", json={"note": "focus on the API"}, headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"] == "start the API"
    assert len(body["applied"]) == 3
    assert body["rejected"] == []
    assert len(body["tasks_created"]) == 1

    agent = client.get("/projects/demo/agents", headers=AUTH).json()
    assert {a["name"] for a in agent} == {"pm", "api-dev"}
    assert str(root / "services" / "api") in [a["config"]["cwd"] for a in agent]

    task = wait_for(client, body["tasks_created"][0])
    assert task["status"] == "done"

    # the manager saw the brief and the roster
    _, prompt = FakeWorker.seen[0]
    assert "Ship the API." in prompt and "pm (manager)" in prompt


def test_plan_rejects_bad_actions_and_applies_the_rest(client, root: Path):
    new_project(client, root)
    FakeWorker.replies["demo__pm"] = [
        json.dumps(
            {
                "summary": "mixed",
                "actions": [
                    {"op": "create_agent", "name": "ok-dev", "allowed_tools": ["Read"]},
                    {"op": "create_agent", "name": "bad-tools", "allowed_tools": ["Bash"]},
                    {"op": "create_agent", "name": "bad-cwd", "cwd": "../../etc"},
                    {"op": "create_task", "agent": "ghost", "title": "T", "text": "x"},
                    {"op": "cancel_task", "task_id": "nope"},
                    {"op": "delete_agent", "name": "pm"},
                ],
            }
        )
    ]
    resp = client.post("/projects/demo/plan", headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert [a["name"] for a in body["applied"]] == ["ok-dev"]
    reasons = " | ".join(r["reason"] for r in body["rejected"])
    assert len(body["rejected"]) == 5
    assert "tool_policy" in reasons
    assert "escapes" in reasons
    assert "unknown agent 'ghost'" in reasons
    assert "unknown task 'nope'" in reasons
    assert "cannot delete itself" in reasons
    # the manager is still there
    assert "demo__pm" in client.app.state.cc.registry.by_name


def test_plan_actions_apply_in_order(client, root: Path):
    """create_task may name an agent created earlier in the same plan."""
    new_project(client, root)
    FakeWorker.replies["demo__pm"] = [
        json.dumps(
            {
                "actions": [
                    {"op": "create_agent", "name": "fresh"},
                    {"op": "create_task", "agent": "fresh", "title": "T", "text": "x"},
                ]
            }
        )
    ]
    body = client.post("/projects/demo/plan", headers=AUTH).json()
    assert body["rejected"] == []
    assert len(body["tasks_created"]) == 1


def test_plan_is_refused_without_a_manager(client, root: Path):
    resp = client.post(
        "/projects", json={"name": "Bare", "root_dir": str(root)}, headers=AUTH
    )
    assert resp.status_code == 201
    resp = client.post("/projects/bare/plan", headers=AUTH)
    assert resp.status_code == 409
    assert "projectmanager" in resp.json()["detail"]


def test_unreadable_plan_is_reported_not_swallowed(client, root: Path):
    new_project(client, root)
    FakeWorker.replies["demo__pm"] = ["I would rather write you a poem."]
    resp = client.post("/projects/demo/plan", headers=AUTH)
    assert resp.status_code == 422
    assert "plan" in resp.json()["detail"]


def test_plan_caps_the_number_of_actions(client, root: Path):
    new_project(client, root)
    actions = [
        {"op": "note", "text": f"n{i}"} for i in range(30)
    ]
    FakeWorker.replies["demo__pm"] = [json.dumps({"actions": actions})]
    body = client.post("/projects/demo/plan", headers=AUTH).json()
    assert len(body["applied"]) == 25
    assert len(body["rejected"]) == 5
    assert "25-action limit" in body["rejected"][0]["reason"]


def test_manager_cannot_appoint_another_manager(client, root: Path):
    """`role` is not part of the action vocabulary, so it cannot be smuggled in."""
    new_project(client, root)
    FakeWorker.replies["demo__pm"] = [
        json.dumps(
            {"actions": [{"op": "create_agent", "name": "pm2", "role": "manager"}]}
        )
    ]
    body = client.post("/projects/demo/plan", headers=AUTH).json()
    assert body["applied"] == []
    assert "not permitted" in body["rejected"][0]["reason"]
    assert [a.name for a in client.app.state.cc.store.list_agents("demo")] == ["pm"]


def test_one_malformed_action_does_not_sink_the_plan(client, root: Path):
    new_project(client, root)
    FakeWorker.replies["demo__pm"] = [
        json.dumps(
            {
                "actions": [
                    {"op": "create_agent"},  # no name
                    {"op": "teleport", "name": "x"},  # not an op at all
                    {"op": "note", "text": "still fine"},
                ]
            }
        )
    ]
    body = client.post("/projects/demo/plan", headers=AUTH).json()
    assert len(body["applied"]) == 1
    assert len(body["rejected"]) == 2


def test_project_routes_need_the_api_key(client, root: Path):
    assert client.get("/projects").status_code == 401
    assert client.post("/projects", json={}).status_code == 401
    assert client.get("/tasks").status_code == 401
