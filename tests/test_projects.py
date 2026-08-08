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

    sent = [text for _, text in FakeWorker.seen]
    assert len(sent) == 1 and sent[0].startswith(f"[task {live}] Live\n\nkept")
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


def test_plan_assigns_backlog_work_instead_of_restating_it(client, root: Path):
    docs = root / "docs"
    docs.mkdir()
    (docs / "console.md").write_text(DOC_WITH_WORK)
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    client.post("/projects/demo/milestones/import", json={}, headers=AUTH)

    backlog = client.get("/projects/demo/tasks?status=backlog", headers=AUTH).json()
    chosen = backlog[0]["id"]
    FakeWorker.replies["demo__pm"] = [
        json.dumps(
            {
                "summary": "the work is already written down",
                "actions": [{"op": "assign_task", "task_id": chosen, "agent": "dev"}],
            }
        )
    ]

    body = client.post("/projects/demo/plan", json=None, headers=AUTH).json()
    assert body["rejected"] == []
    assert body["tasks_created"] == [chosen]

    # The manager was shown the backlog it was supposed to draw from.
    _, prompt = FakeWorker.seen[0]
    assert "# Backlog" in prompt and chosen in prompt

    task = wait_for(client, chosen)
    assert task["status"] == "done" and task["agent"] == "demo__dev"


def test_a_manager_cannot_assign_backlog_work_to_itself(client, root: Path):
    new_project(client, root)
    task = client.post(
        "/projects/demo/tasks", json={"title": "Do it", "text": "now"}, headers=AUTH
    ).json()
    FakeWorker.replies["demo__pm"] = [
        json.dumps({"actions": [{"op": "assign_task", "task_id": task["id"], "agent": "pm"}]})
    ]

    body = client.post("/projects/demo/plan", json=None, headers=AUTH).json()
    assert body["applied"] == []
    assert "may not assign tasks to itself" in body["rejected"][0]["reason"]
    assert client.get(f"/tasks/{task['id']}", headers=AUTH).json()["status"] == "backlog"


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


# --- issues ----------------------------------------------------------------- #


def test_issue_is_raised_and_listed(client, root: Path):
    new_project(client, root)
    resp = client.post(
        "/projects/demo/issues",
        json={"title": "Which database?", "body": "Postgres or SQLite.", "kind": "decision"},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    issue = resp.json()
    assert issue["status"] == "open" and issue["created_by"] == "api"

    assert client.get("/projects/demo/issues", headers=AUTH).json()[0]["id"] == issue["id"]
    assert client.get("/issues?status=open", headers=AUTH).json()[0]["id"] == issue["id"]
    assert client.get("/projects/demo", headers=AUTH).json()["open_issues"] == 1


def test_issue_resolve_and_dismiss(client, root: Path):
    new_project(client, root)
    def raise_one(title):
        return client.post(
            "/projects/demo/issues", json={"title": title}, headers=AUTH
        ).json()["id"]

    a, b = raise_one("first"), raise_one("second")
    resolved = client.post(
        f"/issues/{a}/resolve", json={"resolution": "chose SQLite"}, headers=AUTH
    ).json()
    assert resolved["status"] == "resolved" and resolved["resolution"] == "chose SQLite"

    dismissed = client.post(f"/issues/{b}/resolve", json={"dismiss": True}, headers=AUTH).json()
    assert dismissed["status"] == "dismissed"

    # closing twice is a conflict, not a silent no-op
    assert client.post(f"/issues/{a}/resolve", json={}, headers=AUTH).status_code == 409
    assert client.get("/issues?status=open", headers=AUTH).json() == []
    assert client.get("/issues/nope", headers=AUTH).status_code == 404


def test_an_issue_naming_an_unknown_agent_is_refused(client, root: Path):
    new_project(client, root)
    resp = client.post(
        "/projects/demo/issues", json={"title": "x", "agent": "ghost"}, headers=AUTH
    )
    assert resp.status_code == 404


def test_a_failed_task_raises_a_crash_issue_by_itself(client, root: Path, monkeypatch):
    """A failed run is the "crash unexpected" case; nobody should have to notice it."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)

    async def broken(self, text, session_id, resume):
        return RunResult(False, "`claude` exited 1: segfault")

    monkeypatch.setattr(FakeWorker, "_spawn", broken)
    tid = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "Build it", "text": "go"},
        headers=AUTH,
    ).json()["id"]
    assert wait_for(client, tid)["status"] == "failed"

    issues = client.get("/issues?kind=crash", headers=AUTH).json()
    assert len(issues) == 1
    assert issues[0]["created_by"] == "system"
    assert issues[0]["task_id"] == tid
    assert issues[0]["agent"] == "demo__dev"
    assert "segfault" in issues[0]["body"]
    assert "Build it" in issues[0]["title"]


def test_a_successful_task_raises_nothing(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    tid = client.post(
        "/projects/demo/tasks", json={"agent": "dev", "title": "T", "text": "go"},
        headers=AUTH,
    ).json()["id"]
    assert wait_for(client, tid)["status"] == "done"
    assert client.get("/issues", headers=AUTH).json() == []


def test_the_manager_can_raise_and_resolve_issues_in_a_plan(client, root: Path):
    new_project(client, root)
    existing = client.post(
        "/projects/demo/issues", json={"title": "old"}, headers=AUTH
    ).json()["id"]

    FakeWorker.replies["demo__pm"] = [
        json.dumps({"actions": [
            {"op": "raise_issue", "title": "Need a decision on auth",
             "body": "OAuth or API keys?", "kind": "decision"},
            {"op": "resolve_issue", "issue_id": existing, "resolution": "settled"},
        ]})
    ]
    body = client.post("/projects/demo/plan", headers=AUTH).json()
    assert body["rejected"] == []
    assert len(body["issues_raised"]) == 1

    raised = client.get(f"/issues/{body['issues_raised'][0]}", headers=AUTH).json()
    assert raised["kind"] == "decision" and raised["created_by"] == "manager"
    assert client.get(f"/issues/{existing}", headers=AUTH).json()["status"] == "resolved"


def test_open_issues_reach_the_manager_prompt(client, root: Path):
    new_project(client, root)
    client.post(
        "/projects/demo/issues",
        json={"title": "Blocked on credentials", "body": "no token for the API"},
        headers=AUTH,
    )
    FakeWorker.replies["demo__pm"] = [json.dumps({"actions": []})]
    client.post("/projects/demo/plan", headers=AUTH)

    _, prompt = FakeWorker.seen[0]
    assert "Open issues" in prompt
    assert "Blocked on credentials" in prompt
    assert "no token for the API" in prompt


def test_a_plan_resolving_another_projects_issue_is_rejected(client, root: Path, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    new_project(client, root)
    new_project(client, other, name="Other")
    foreign = client.post(
        "/projects/other/issues", json={"title": "theirs"}, headers=AUTH
    ).json()["id"]

    FakeWorker.replies["demo__pm"] = [
        json.dumps({"actions": [{"op": "resolve_issue", "issue_id": foreign}]})
    ]
    body = client.post("/projects/demo/plan", headers=AUTH).json()
    assert body["applied"] == []
    assert "unknown issue" in body["rejected"][0]["reason"]
    assert client.get(f"/issues/{foreign}", headers=AUTH).json()["status"] == "open"


# --- github link and overview seeding --------------------------------------- #


def test_project_stores_a_canonical_github_link(client, root: Path):
    body = new_project(client, root, github_url="git@github.com:owner/repo.git")
    assert body["github_url"] == "https://github.com/owner/repo"


def test_a_bad_github_link_is_refused(client, root: Path):
    resp = client.post(
        "/projects",
        json={"name": "Bad", "root_dir": str(root), "github_url": "https://gitlab.com/o/r"},
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "GitHub" in resp.json()["detail"]


def test_github_link_can_be_set_and_cleared_after_creation(client, root: Path):
    new_project(client, root)
    patched = client.patch(
        "/projects/demo", json={"github_url": "owner/repo"}, headers=AUTH
    ).json()
    assert patched["github_url"] == "https://github.com/owner/repo"

    cleared = client.patch("/projects/demo", json={"github_url": ""}, headers=AUTH).json()
    assert cleared["github_url"] is None
    assert client.patch(
        "/projects/demo", json={"github_url": "nope!!"}, headers=AUTH
    ).status_code == 422


def test_creating_a_project_seeds_overview_from_a_local_readme(client, tmp_path: Path):
    """The working copy wins over the network, and needs no token."""
    root = tmp_path / "seeded"
    root.mkdir()
    (root / "README.md").write_text("# From the repo\n\nWhat this is for.\n")

    new_project(client, root, name="Seeded", github_url="owner/repo")
    assert (root / "overview.md").read_text() == "# From the repo\n\nWhat this is for.\n"
    assert "From the repo" in client.get("/projects/seeded/overview", headers=AUTH).text


def test_seeding_never_overwrites_an_existing_brief_implicitly(client, root: Path):
    (root / "README.md").write_text("# Readme wins?\n")
    new_project(client, root)  # the fixture root already has overview.md
    assert "Ship the API" in (root / "overview.md").read_text()

    resp = client.post("/projects/demo/overview/sync", json={}, headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["written"] is False
    assert "already exists" in resp.json()["detail"]

    forced = client.post(
        "/projects/demo/overview/sync", json={"overwrite": True}, headers=AUTH
    ).json()
    assert forced["written"] is True
    assert (root / "overview.md").read_text() == "# Readme wins?\n"


def test_sync_reports_when_there_is_nothing_to_sync_from(client, root: Path):
    new_project(client, root)
    body = client.post(
        "/projects/demo/overview/sync", json={"overwrite": True}, headers=AUTH
    ).json()
    assert body["written"] is False
    assert "no README" in body["detail"]


# --- branch ----------------------------------------------------------------- #


def test_branch_rides_on_tasks_issues_and_log_entries(client, root: Path, home: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)

    tid = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "T", "text": "go", "branch": "feat/api"},
        headers=AUTH,
    ).json()["id"]
    assert wait_for(client, tid)["branch"] == "feat/api"

    iid = client.post(
        "/projects/demo/issues",
        json={"title": "blocked", "branch": "feat/api"},
        headers=AUTH,
    ).json()["id"]
    assert client.get(f"/issues/{iid}", headers=AUTH).json()["branch"] == "feat/api"

    dates = client.get("/logs", headers=AUTH).json()["dates"]
    entry = client.get(f"/logs/{dates[-1]}/demo", headers=AUTH).text
    assert "| branch: feat/api" in entry


def test_tasks_and_issues_filter_by_branch(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    for branch in ("feat/a", "feat/b"):
        client.post(
            "/projects/demo/tasks",
            json={"agent": "dev", "title": branch, "text": "x", "branch": branch},
            headers=AUTH,
        )
        client.post(
            "/projects/demo/issues", json={"title": branch, "branch": branch}, headers=AUTH
        )

    tasks = client.get("/tasks?branch=feat/a", headers=AUTH).json()
    assert [t["title"] for t in tasks] == ["feat/a"]
    issues = client.get("/issues?branch=feat/b", headers=AUTH).json()
    assert [i["title"] for i in issues] == ["feat/b"]


def test_a_crash_issue_inherits_the_failed_task_branch(client, root: Path, monkeypatch):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)

    async def broken(self, text, session_id, resume):
        return RunResult(False, "boom")

    monkeypatch.setattr(FakeWorker, "_spawn", broken)
    tid = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "T", "text": "go", "branch": "fix/crash"},
        headers=AUTH,
    ).json()["id"]
    wait_for(client, tid)

    issue = client.get("/issues?kind=crash", headers=AUTH).json()[0]
    assert issue["branch"] == "fix/crash" and issue["task_id"] == tid


def test_the_manager_can_set_a_branch_in_a_plan(client, root: Path):
    new_project(client, root)
    FakeWorker.replies["demo__pm"] = [
        json.dumps({"actions": [
            {"op": "create_agent", "name": "dev"},
            {"op": "create_task", "agent": "dev", "title": "T", "text": "x",
             "branch": "feat/from-plan"},
            {"op": "raise_issue", "title": "blocked", "branch": "feat/from-plan"},
        ]})
    ]
    body = client.post("/projects/demo/plan", headers=AUTH).json()
    assert body["rejected"] == []
    task = client.get(f"/tasks/{body['tasks_created'][0]}", headers=AUTH).json()
    assert task["branch"] == "feat/from-plan"
    issue = client.get(f"/issues/{body['issues_raised'][0]}", headers=AUTH).json()
    assert issue["branch"] == "feat/from-plan"


# --- integrated search ------------------------------------------------------ #


def test_search_spans_tasks_issues_and_logs(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    FakeWorker.replies["demo__dev"] = ["migrated the schema"]
    tid = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "Run the migration", "text": "alter the table",
              "branch": "feat/db"},
        headers=AUTH,
    ).json()["id"]
    wait_for(client, tid)
    client.post(
        "/projects/demo/issues",
        json={"title": "migration needs a decision", "body": "which column order?",
              "branch": "feat/db"},
        headers=AUTH,
    )

    hits = client.get("/search?q=migrat", headers=AUTH).json()
    kinds = {h["type"] for h in hits}
    assert kinds == {"task", "issue", "log"}, kinds
    assert all(h["created_at"] for h in hits)
    # Newest first across all three kinds. Compared as datetimes, not strings:
    # log entries are stamped to the second and rows to the microsecond, so
    # lexical order is not chronological order.
    from datetime import datetime

    stamps = [datetime.fromisoformat(h["created_at"]) for h in hits]
    assert stamps == sorted(stamps, reverse=True)


def test_search_filters_by_type_branch_and_agent(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    tid = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "Ship it", "text": "x", "branch": "feat/db"},
        headers=AUTH,
    ).json()["id"]
    wait_for(client, tid)
    client.post(
        "/projects/demo/issues",
        json={"title": "Ship it too", "branch": "other"}, headers=AUTH
    )

    only_tasks = client.get("/search?type=task", headers=AUTH).json()
    assert {h["type"] for h in only_tasks} == {"task"}

    on_branch = client.get("/search?branch=feat/db", headers=AUTH).json()
    assert on_branch and all(h["branch"] == "feat/db" for h in on_branch)

    by_agent = client.get("/search?agent=demo__dev&type=task,log", headers=AUTH).json()
    assert by_agent and all(h["agent"] == "demo__dev" for h in by_agent)

    # an unknown type is ignored rather than erroring, so a stale link still works
    assert client.get("/search?type=nonsense", headers=AUTH).status_code == 200


def test_search_scopes_to_a_project(client, root: Path, tmp_path: Path):
    other = tmp_path / "other"
    other.mkdir()
    new_project(client, root)
    new_project(client, other, name="Other")
    for pid in ("demo", "other"):
        client.post(f"/projects/{pid}/issues", json={"title": "shared word"}, headers=AUTH)

    scoped = client.get("/search?q=shared&project=demo", headers=AUTH).json()
    assert {h["project_id"] for h in scoped} == {"demo"}
    assert len(client.get("/search?q=shared", headers=AUTH).json()) == 2


def test_search_needs_the_api_key(client):
    assert client.get("/search").status_code == 401


# --- creating a project with its roster ------------------------------------- #


def test_a_project_can_be_created_with_its_agents(client, root: Path):
    resp = client.post(
        "/projects",
        json={
            "name": "Staffed",
            "root_dir": str(root),
            "manager": {"name": "pm", "role": "manager", "allowed_tools": ["Read"]},
            "agents": [
                {"name": "api-dev", "allowed_tools": ["Read", "Edit"], "cwd": "services/api"},
                {"name": "tester", "allowed_tools": ["Read"]},
            ],
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["agents"] == ["api-dev", "pm", "tester"]

    agents = {a["name"]: a for a in client.get("/projects/staffed/agents", headers=AUTH).json()}
    assert agents["pm"]["role"] == "manager"
    assert agents["api-dev"]["role"] == "worker"
    assert agents["api-dev"]["config"]["cwd"].endswith("services/api")
    # every one of them is routable straight away, without a restart
    registry = client.app.state.cc.registry.by_name
    assert {"staffed__pm", "staffed__api-dev", "staffed__tester"} <= set(registry)


def test_an_agent_listed_at_creation_is_forced_to_worker(client, root: Path):
    """`agents` cannot smuggle in a second manager."""
    resp = client.post(
        "/projects",
        json={
            "name": "Forced", "root_dir": str(root),
            "manager": {"name": "pm", "role": "manager"},
            "agents": [{"name": "sneaky", "role": "manager"}],
        },
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    agents = {a["name"]: a["role"] for a in client.get("/projects/forced/agents", headers=AUTH).json()}
    assert agents == {"pm": "manager", "sneaky": "worker"}


def test_one_bad_agent_unwinds_the_whole_project(client, root: Path):
    """Half a roster is not the project that was asked for."""
    resp = client.post(
        "/projects",
        json={
            "name": "Doomed", "root_dir": str(root),
            "manager": {"name": "pm", "role": "manager"},
            "agents": [
                {"name": "fine"},
                {"name": "bad", "allowed_tools": ["Bash"]},  # outside the policy
            ],
        },
        headers=AUTH,
    )
    assert resp.status_code == 422
    assert "Bash" in resp.json()["detail"]
    assert client.get("/projects/doomed", headers=AUTH).status_code == 404
    assert "doomed__fine" not in client.app.state.cc.registry.by_name


def test_a_project_can_still_be_created_bare(client, root: Path):
    resp = client.post(
        "/projects", json={"name": "Bare", "root_dir": str(root)}, headers=AUTH
    )
    assert resp.status_code == 201
    assert resp.json()["agents"] == [] and resp.json()["manager"] is None


# --- agent state ------------------------------------------------------------ #


def test_agent_state_reports_idle_then_what_it_is_working_on(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev", "model": "haiku"},
                headers=AUTH)

    idle = client.get("/agents/demo__dev", headers=AUTH).json()
    assert idle["activity"] == "idle" and idle["working_on"] is None
    assert idle["model"] == "haiku" and idle["tasks_done"] == 0

    tid = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "Review the auth module", "text": "look at it"},
        headers=AUTH,
    ).json()["id"]
    wait_for(client, tid)

    after = client.get("/agents/demo__dev", headers=AUTH).json()
    assert after["tasks_done"] == 1 and after["cost_usd"] > 0
    assert after["last_active"] and after["recent"][0]["id"] == tid
    assert after["activity"] == "idle"  # the run is over


def test_agent_state_is_404_for_an_unknown_agent(client, root: Path):
    assert client.get("/agents/ghost", headers=AUTH).status_code == 404


def test_activity_is_inferred_from_the_task_words():
    from server.models import infer_activity

    assert infer_activity("Review the auth module") == "reviewing"
    assert infer_activity("Add the ping route") == "coding"
    assert infer_activity("Run the test suite") == "testing"
    assert infer_activity("Investigate the crash") == "researching"
    assert infer_activity("Deploy on Friday") == "working"  # honest fallback


def test_the_agents_list_carries_the_same_state(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    rows = {a["name"]: a for a in client.get("/agents", headers=AUTH).json()}
    assert rows["demo__dev"]["activity"] == "idle"
    assert rows["demo__pm"]["role"] == "manager"


# --- importing milestones from documents ------------------------------------ #


def test_milestones_import_from_the_docs_directory(client, root: Path):
    docs = root / "docs"
    docs.mkdir()
    (docs / "auth.md").write_text("# Ship authentication\n\nOAuth, then keys.\n")
    (docs / "api.md").write_text("Prose first, no heading.\n")
    (docs / "README.md").write_text("# Not a goal\n")   # describes, not a goal
    (docs / "notes.txt").write_text("ignored, not markdown")
    new_project(client, root)

    dry = client.post(
        "/projects/demo/milestones/import", json={"dry_run": True}, headers=AUTH
    ).json()
    assert dry["dry_run"] is True
    assert {i["title"] for i in dry["imported"]} == {"Ship authentication", "Api"}
    assert client.get("/projects/demo/milestones", headers=AUTH).json() == []

    real = client.post("/projects/demo/milestones/import", json={}, headers=AUTH).json()
    assert len(real["imported"]) == 2
    milestones = {m["title"]: m for m in client.get("/projects/demo/milestones", headers=AUTH).json()}
    assert milestones["Ship authentication"]["source"] == "docs/auth.md"
    assert "OAuth, then keys." in milestones["Ship authentication"]["body"]
    assert milestones["Ship authentication"]["created_by"] == "user"


def test_importing_twice_does_not_duplicate(client, root: Path):
    docs = root / "docs"
    docs.mkdir()
    (docs / "one.md").write_text("# Only once\n")
    new_project(client, root)

    client.post("/projects/demo/milestones/import", json={}, headers=AUTH)
    again = client.post("/projects/demo/milestones/import", json={}, headers=AUTH).json()
    assert again["imported"] == []
    assert again["skipped"][0]["skipped"] == "already imported"
    assert len(client.get("/projects/demo/milestones", headers=AUTH).json()) == 1


def test_import_accepts_files_and_directories(client, root: Path):
    (root / "plan.md").write_text("# From a file\n")
    extra = root / "specs"
    extra.mkdir()
    (extra / "b.md").write_text("# From a directory\n")
    new_project(client, root)

    body = client.post(
        "/projects/demo/milestones/import",
        json={"paths": ["plan.md", "specs"]}, headers=AUTH,
    ).json()
    assert {i["title"] for i in body["imported"]} == {"From a file", "From a directory"}
    assert body["scanned"] == ["plan.md", "specs/"]


def test_import_refuses_a_path_outside_the_project(client, root: Path):
    new_project(client, root)
    resp = client.post(
        "/projects/demo/milestones/import", json={"paths": ["../../etc"]}, headers=AUTH
    )
    assert resp.status_code == 422
    assert "escapes" in resp.json()["detail"]


def test_import_reports_a_missing_directory(client, root: Path):
    new_project(client, root)
    resp = client.post(
        "/projects/demo/milestones/import", json={"paths": ["nope"]}, headers=AUTH
    )
    assert resp.status_code == 404


# --- importing tasks and issues out of the same documents ------------------- #

DOC_WITH_WORK = """\
# Ship the console

Prose that is not a task and must stay out of the backlog.

## 3. Task Breakdown

1. **Scaffold** — the app shell and routing.
2. Wire the API client.
   Uses the key from the header.
3. no

## Acceptance criteria

- [ ] The console lists projects.
- [x] The key dialog exists already.

## Open questions

- Which agent owns deploys?

## Risks

- The KDS backend may not be up.
"""


def test_import_pulls_tasks_and_issues_out_of_a_document(client, root: Path):
    docs = root / "docs"
    docs.mkdir()
    (docs / "console.md").write_text(DOC_WITH_WORK)
    new_project(client, root)

    body = client.post("/projects/demo/milestones/import", json={}, headers=AUTH).json()
    entry = body["imported"][0]
    assert entry["title"] == "Ship the console"
    # Three numbered items minus the one too short to be a task, plus the one
    # unticked acceptance criterion. The ticked one is history, not work.
    assert entry["tasks"] == 3
    assert entry["issues"] == 2
    assert len(body["tasks_created"]) == 3
    assert len(body["issues_created"]) == 2

    tasks = client.get("/projects/demo/tasks?status=backlog", headers=AUTH).json()
    titles = {t["title"] for t in tasks}
    assert titles == {
        "Scaffold — the app shell and routing.",
        "Wire the API client.",
        "The console lists projects.",
    }
    one = next(t for t in tasks if t["title"] == "Wire the API client.")
    assert one["agent"] is None and one["message_id"] is None
    assert one["created_by"] == "import"
    assert one["milestone_id"] == entry["milestone_id"]
    # The instruction carries the nested line and a pointer back to the source.
    assert "Uses the key from the header." in one["text"]
    assert "docs/console.md § 3. Task Breakdown" in one["text"]

    issues = client.get("/projects/demo/issues", headers=AUTH).json()
    by_title = {i["title"]: i for i in issues}
    assert by_title["Which agent owns deploys?"]["kind"] == "decision"
    assert by_title["The KDS backend may not be up."]["kind"] == "blocker"
    assert all(i["created_by"] == "import" for i in issues)

    # Nothing was dispatched: a backlog task has no queue entry.
    assert FakeWorker.seen == []


def test_import_can_be_asked_for_milestones_only(client, root: Path):
    docs = root / "docs"
    docs.mkdir()
    (docs / "console.md").write_text(DOC_WITH_WORK)
    new_project(client, root)

    body = client.post(
        "/projects/demo/milestones/import",
        json={"tasks": False, "issues": False}, headers=AUTH,
    ).json()
    assert body["tasks_created"] == [] and body["issues_created"] == []
    assert client.get("/projects/demo/tasks", headers=AUTH).json() == []
    assert len(client.get("/projects/demo/milestones", headers=AUTH).json()) == 1


def test_a_dry_run_counts_the_work_without_writing_it(client, root: Path):
    docs = root / "docs"
    docs.mkdir()
    (docs / "console.md").write_text(DOC_WITH_WORK)
    new_project(client, root)

    body = client.post(
        "/projects/demo/milestones/import", json={"dry_run": True}, headers=AUTH
    ).json()
    assert body["imported"][0]["tasks"] == 3
    assert body["imported"][0]["issues"] == 2
    assert body["tasks_created"] == [] and body["issues_created"] == []
    assert client.get("/projects/demo/tasks", headers=AUTH).json() == []


def test_a_project_counts_its_backlog_apart_from_its_open_work(client, root: Path):
    docs = root / "docs"
    docs.mkdir()
    (docs / "console.md").write_text(DOC_WITH_WORK)
    new_project(client, root)
    client.post("/projects/demo/milestones/import", json={}, headers=AUTH)

    info = client.get("/projects/demo", headers=AUTH).json()
    assert info["backlog_tasks"] == 3
    assert info["open_tasks"] == 0

    insight = client.get("/projects/demo/insight", headers=AUTH).json()
    assert insight["tasks_by_status"]["backlog"] == 3
    assert insight["milestones"][0]["tasks_backlog"] == 3


# --- assigning backlog work -------------------------------------------------- #


def backlog_task(client, root: Path) -> dict:
    """A project with one worker and one unassigned task."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    resp = client.post(
        "/projects/demo/tasks",
        json={"title": "Write the docs", "text": "All of them"},
        headers=AUTH,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_a_task_without_an_agent_lands_in_the_backlog(client, root: Path):
    task = backlog_task(client, root)
    assert task["status"] == "backlog"
    assert task["agent"] is None and task["message_id"] is None
    assert FakeWorker.seen == []


def test_assigning_a_backlog_task_queues_it(client, root: Path):
    task = backlog_task(client, root)
    resp = client.post(
        f"/projects/demo/tasks/{task['id']}/assign",
        json={"agent": "dev", "branch": "feat/docs"}, headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assigned = resp.json()
    assert assigned["agent"] == "demo__dev"
    assert assigned["branch"] == "feat/docs"
    assert assigned["message_id"]

    done = wait_for(client, task["id"])
    assert done["status"] == "done"
    assert any(t == "demo__dev" for t, _ in FakeWorker.seen)


def test_a_task_can_only_be_assigned_once(client, root: Path):
    task = backlog_task(client, root)
    first = client.post(
        f"/projects/demo/tasks/{task['id']}/assign", json={"agent": "dev"}, headers=AUTH
    )
    assert first.status_code == 200
    again = client.post(
        f"/projects/demo/tasks/{task['id']}/assign", json={"agent": "dev"}, headers=AUTH
    )
    assert again.status_code == 409
    assert "only a backlog task" in again.json()["detail"]


def test_assigning_to_an_unknown_agent_is_a_404(client, root: Path):
    task = backlog_task(client, root)
    resp = client.post(
        f"/projects/demo/tasks/{task['id']}/assign", json={"agent": "ghost"}, headers=AUTH
    )
    assert resp.status_code == 404
    assert client.get(f"/tasks/{task['id']}", headers=AUTH).json()["status"] == "backlog"


def test_a_backlog_task_can_be_cancelled(client, root: Path):
    task = backlog_task(client, root)
    resp = client.post(f"/tasks/{task['id']}/cancel", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


# --- a worker asking the operator something --------------------------------- #


def test_a_worker_can_raise_a_question_as_an_issue(client, root: Path):
    """A run cannot wait for an answer, so the question outlives the run."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    milestone = client.post(
        "/projects/demo/milestones", json={"title": "Ship it"}, headers=AUTH
    ).json()
    FakeWorker.replies["demo__dev"] = [
        "Wrote the migration.\n\n```ask\nSQLite or KDS for v2?\n"
        "KDS needs a second process.\n```\n\nDone otherwise."
    ]

    task = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "Migrate", "text": "do it",
              "branch": "feat/store", "milestone_id": milestone["id"]},
        headers=AUTH,
    ).json()
    assert wait_for(client, task["id"])["status"] == "done"

    issues = client.get("/projects/demo/issues", headers=AUTH).json()
    asked = next(i for i in issues if i["title"] == "SQLite or KDS for v2?")
    assert asked["kind"] == "decision" and asked["created_by"] == "agent"
    assert asked["agent"] == "demo__dev"
    # It hangs where the work did, so the board keeps its shape.
    assert asked["task_id"] == task["id"]
    assert asked["branch"] == "feat/store"
    assert asked["milestone_id"] == milestone["id"]
    assert "second process" in asked["body"]

    # Asking does not replace answering: the result is still the result.
    assert "Wrote the migration." in client.get(f"/tasks/{task['id']}", headers=AUTH).json()["result"]


def test_every_task_carries_the_convention_for_asking(client, root: Path):
    """A worker cannot use a protocol nobody told it about, and it may have been
    created without a system prompt."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    task = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "Look", "text": "at it"}, headers=AUTH,
    ).json()
    wait_for(client, task["id"])

    _, prompt = FakeWorker.seen[0]
    assert "```ask" in prompt and "at it" in prompt


def test_a_reply_with_no_question_raises_nothing(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    FakeWorker.replies["demo__dev"] = ["Should I have used KDS? I used SQLite."]
    task = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "Migrate", "text": "do it"}, headers=AUTH,
    ).json()
    wait_for(client, task["id"])
    assert client.get("/projects/demo/issues", headers=AUTH).json() == []


def test_a_failed_run_reports_the_crash_rather_than_its_questions(client, root: Path):
    """One failure, one issue. A crash is not the moment to also file questions."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)

    async def boom(self, text, session_id, resume):
        return RunResult(ok=False, result_text="```ask\nWhy did I break?\n```",
                         session_id=session_id, cost_usd=0.0, duration_s=0.1)

    original, FakeWorker._spawn = FakeWorker._spawn, boom
    try:
        task = client.post(
            "/projects/demo/tasks",
            json={"agent": "dev", "title": "Break", "text": "it"}, headers=AUTH,
        ).json()
        wait_for(client, task["id"])
    finally:
        FakeWorker._spawn = original

    kinds = [i["kind"] for i in client.get("/projects/demo/issues", headers=AUTH).json()]
    assert kinds == ["crash"]


def test_the_operators_answer_reaches_the_manager(client, root: Path):
    """The console promises the manager reads a resolution. Without this it is a
    lie: a resolved issue leaves the open list and is never seen again."""
    new_project(client, root)
    issue = client.post(
        "/projects/demo/issues",
        json={"title": "SQLite or KDS?", "kind": "decision"}, headers=AUTH,
    ).json()
    client.post(
        f"/issues/{issue['id']}/resolve",
        json={"resolution": "SQLite for now; revisit after v2."}, headers=AUTH,
    )

    FakeWorker.replies["demo__pm"] = [json.dumps({"actions": []})]
    client.post("/projects/demo/plan", json=None, headers=AUTH)

    _, prompt = FakeWorker.seen[0]
    assert "# Answered" in prompt
    assert "SQLite for now; revisit after v2." in prompt
    # And it is no longer presented as blocking.
    assert prompt.index("# Open issues") < prompt.index("# Answered")


# --- changing an agent without recreating it -------------------------------- #


def test_an_agents_settings_can_be_changed_in_place(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)

    resp = client.patch(
        "/projects/demo/agents/dev",
        json={"max_budget_usd": 2.5, "timeout_s": 1200, "model": "opus"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    config = resp.json()["config"]
    assert config["max_budget_usd"] == 2.5
    assert config["timeout_s"] == 1200 and config["model"] == "opus"

    # Both the store and the live registry moved, not just one of them.
    assert client.app.state.cc.registry.by_name["demo__dev"].max_budget_usd == 2.5
    reloaded = client.get("/projects/demo/agents", headers=AUTH).json()
    assert next(a for a in reloaded if a["name"] == "dev")["config"]["timeout_s"] == 1200


def test_the_session_and_the_queue_survive_a_patch(client, root: Path):
    """The point of patching rather than recreating: the runtime name is what
    the session is filed under, and a rebuild would strand the conversation."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    client.app.state.cc.sessions.set("demo__dev", "sess-keep")

    client.patch("/projects/demo/agents/dev", json={"max_budget_usd": 1.0}, headers=AUTH)

    assert client.app.state.cc.sessions.get("demo__dev") == "sess-keep"
    assert client.app.state.cc.dispatcher.depth("demo__dev") == 0  # queue still there
    assert "demo__dev" in client.app.state.cc.pool.workers


def test_a_patch_cannot_grant_tools_the_policy_forbids(client, root: Path):
    """The same gate a new agent goes through — there is no second path in."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    resp = client.patch(
        "/projects/demo/agents/dev", json={"allowed_tools": ["Read", "Bash"]}, headers=AUTH
    )
    assert resp.status_code == 422 and "Bash" in resp.json()["detail"]
    assert "Bash" not in client.app.state.cc.registry.by_name["demo__dev"].allowed_tools


def test_omitted_fields_are_left_alone_and_empty_strings_clear(client, root: Path):
    new_project(client, root)
    client.post(
        "/projects/demo/agents",
        json={"name": "dev", "system_prompt": "be terse", "model": "haiku"},
        headers=AUTH,
    )
    # Touch one field; the others must not move.
    kept = client.patch(
        "/projects/demo/agents/dev", json={"timeout_s": 60}, headers=AUTH
    ).json()["config"]
    assert kept["system_prompt"] == "be terse" and kept["model"] == "haiku"

    cleared = client.patch(
        "/projects/demo/agents/dev", json={"system_prompt": "", "model": ""}, headers=AUTH
    ).json()["config"]
    assert cleared["system_prompt"] is None and cleared["model"] is None
    assert cleared["timeout_s"] == 60


def test_a_patch_cannot_rename_an_agent_or_move_it(client, root: Path):
    """Name, role and cwd are not settings: they are what it is keyed by, what
    the database constrains, and where its project lives."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    for body in ({"name": "other"}, {"role": "manager"}, {"cwd": "services/api"}):
        resp = client.patch("/projects/demo/agents/dev", json=body, headers=AUTH)
        assert resp.status_code == 422, f"{body} was accepted"


def test_patching_an_unknown_agent_is_a_404(client, root: Path):
    new_project(client, root)
    resp = client.patch(
        "/projects/demo/agents/ghost", json={"timeout_s": 60}, headers=AUTH
    )
    assert resp.status_code == 404


def test_a_patched_agent_is_rewritten_into_the_cls_directory(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    client.patch("/projects/demo/agents/dev", json={"max_budget_usd": 3.0}, headers=AUTH)
    assert snapshot(root, "dev")["config"]["max_budget_usd"] == 3.0


# --- changing what a project's agents may be granted ------------------------ #


def test_the_tool_policy_can_be_widened(client, root: Path):
    new_project(client, root)
    resp = client.patch(
        "/projects/demo",
        json={"tool_policy": ["Read", "Glob", "Grep", "Edit", "Write", "Bash"]},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    assert "Bash" in resp.json()["tool_policy"]

    # An agent made after it may now be given the new tool.
    added = client.post(
        "/projects/demo/agents",
        json={"name": "dev", "allowed_tools": ["Read", "Bash"]}, headers=AUTH,
    )
    assert added.status_code == 201, added.text


def test_widening_grants_nothing_to_agents_that_already_exist(client, root: Path):
    """An agent's tools were fixed when it was created; the policy is a ceiling,
    not a live grant."""
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    before = client.app.state.cc.registry.by_name["demo__dev"].allowed_tools

    client.patch("/projects/demo", json={"tool_policy": ["Read", "Bash"]}, headers=AUTH)
    assert client.app.state.cc.registry.by_name["demo__dev"].allowed_tools == before
    assert "Bash" not in before


def test_narrowing_below_what_an_agent_holds_is_refused(client, root: Path):
    """A policy its own agents violate is not the truth about the project."""
    new_project(client, root)
    client.post(
        "/projects/demo/agents",
        json={"name": "dev", "allowed_tools": ["Read", "Write"]}, headers=AUTH,
    )
    resp = client.patch("/projects/demo", json={"tool_policy": ["Read"]}, headers=AUTH)
    assert resp.status_code == 409
    assert "dev" in resp.json()["detail"] and "Write" in resp.json()["detail"]
    # and nothing moved
    assert "Write" in client.get("/projects/demo", headers=AUTH).json()["tool_policy"]


def test_an_empty_or_invented_tool_policy_is_refused(client, root: Path):
    new_project(client, root)
    empty = client.patch("/projects/demo", json={"tool_policy": []}, headers=AUTH)
    assert empty.status_code == 422 and "empty" in empty.json()["detail"]

    unknown = client.patch(
        "/projects/demo", json={"tool_policy": ["Read", "Telepathy"]}, headers=AUTH
    )
    assert unknown.status_code == 422 and "Telepathy" in unknown.json()["detail"]


def test_a_narrowing_that_takes_nothing_away_is_allowed(client, root: Path):
    """`Bash(ls *)` is a use of `Bash`; the check is on base names."""
    new_project(client, root, name="Shell", tool_policy=["Read", "Bash"],
                manager={"name": "pm", "role": "manager", "allowed_tools": ["Bash(ls *)"]})
    resp = client.patch("/projects/shell", json={"tool_policy": ["Bash"]}, headers=AUTH)
    assert resp.status_code == 200, resp.text


# --- the cls/ mirror inside the project ------------------------------------- #


def snapshot(root: Path, name: str, *, settled: bool = False) -> dict:
    """The agent's file on disk. `settled` waits for the worker to go idle.

    A task reaches `done` in `job_finished`, which runs while the worker still
    holds the job; the idle snapshot is written just after. Polling here is what
    keeps that ordering from being a race in the test rather than in the server.
    """
    path = root / "cls" / "agents" / f"{name}.json"
    deadline = time.time() + 10
    while True:
        assert path.is_file(), f"no snapshot at {path}"
        body = json.loads(path.read_text())
        if not settled or body["activity"]["state"] == "idle":
            return body
        if time.time() > deadline:
            raise AssertionError(f"{name} stayed {body['activity']['state']}")
        time.sleep(0.02)


def test_an_agent_is_written_into_the_project_when_it_is_created(client, root: Path):
    new_project(client, root)
    assert (root / "cls" / "README.md").is_file()

    pm = snapshot(root, "pm")
    assert pm["agent"] == "pm" and pm["runtime_name"] == "demo__pm"
    assert pm["project"] == "demo" and pm["role"] == "manager"
    assert pm["config"]["allowed_tools"] == ["Read", "Glob", "Grep"]
    assert pm["activity"]["state"] == "idle" and pm["activity"]["busy"] is False
    assert pm["queue"] == {"depth": 0, "running": None, "queued": []}
    assert pm["totals"]["tasks_done"] == 0
    assert pm["updated_at"]


def test_the_snapshot_follows_the_agent_through_a_task(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    task = client.post(
        "/projects/demo/tasks",
        json={"agent": "dev", "title": "Write the docs", "text": "All of them"},
        headers=AUTH,
    ).json()
    wait_for(client, task["id"])

    dev = snapshot(root, "dev", settled=True)
    assert dev["totals"]["tasks_done"] == 1
    assert dev["totals"]["cost_usd"] == pytest.approx(0.01)
    assert dev["totals"]["last_active"]
    recent = dev["recent_tasks"][0]
    assert recent["id"] == task["id"] and recent["status"] == "done"
    assert recent["outcome"].startswith("done: [task ")
    assert dev["queue"]["running"] is None and dev["activity"]["state"] == "idle"


def test_a_failed_run_shows_up_in_the_snapshot_with_its_issue(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)

    async def boom(self, text, session_id, resume):
        return RunResult(
            ok=False, result_text="exploded", session_id=session_id,
            cost_usd=0.02, duration_s=0.1,
        )

    original, FakeWorker._spawn = FakeWorker._spawn, boom
    try:
        task = client.post(
            "/projects/demo/tasks",
            json={"agent": "dev", "title": "Break it", "text": "please"},
            headers=AUTH,
        ).json()
        wait_for(client, task["id"])
    finally:
        FakeWorker._spawn = original

    dev = snapshot(root, "dev", settled=True)
    assert dev["totals"]["tasks_failed"] == 1
    assert dev["recent_tasks"][0]["outcome"] == "exploded"
    assert dev["open_issues"][0]["kind"] == "crash"
    assert dev["open_issues"][0]["title"] == "Task failed: Break it"


def test_deleting_an_agent_takes_its_snapshot(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    assert (root / "cls" / "agents" / "dev.json").is_file()

    client.delete("/projects/demo/agents/dev", headers=AUTH)
    assert not (root / "cls" / "agents" / "dev.json").exists()
    assert (root / "cls" / "agents" / "pm.json").is_file()


def test_a_yaml_agent_has_no_project_to_be_written_into(client, root: Path, home: Path):
    """`alpha` comes from agents.yaml and belongs to no project directory."""
    new_project(client, root)
    resp = client.post("/messages", json={"text": "hello", "tags": ["alpha"]}, headers=AUTH)
    assert resp.status_code == 202
    assert not (home / "work" / "cls").exists()
    assert sorted(p.name for p in (root / "cls" / "agents").iterdir()) == ["pm.json"]


def test_an_unwritable_project_root_does_not_fail_the_request(client, root: Path):
    """The mirror is bookkeeping; it must never take down what triggered it."""
    new_project(client, root)
    (root / "cls").chmod(0o500)
    try:
        resp = client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
        assert resp.status_code == 201, resp.text
    finally:
        (root / "cls").chmod(0o700)


# --- settings and credentials ----------------------------------------------- #


def test_the_token_is_write_only(client, root: Path):
    """It must never come back out of the API, on any route."""
    new_project(client, root)
    client.put(
        "/projects/demo/settings",
        json={"github_token": "ghp_supersecrettoken1234", "allow_push": True,
              "git_user_name": "cls bot", "git_user_email": "bot@example.com"},
        headers=AUTH,
    )
    body = client.get("/projects/demo/settings", headers=AUTH).json()
    assert body["github_token_set"] is True
    assert body["github_token_hint"] == "…1234"
    assert "ghp_supersecrettoken1234" not in json.dumps(body)
    assert body["git_user_name"] == "cls bot" and body["allow_push"] is True

    # and not through any other project route either
    for path in ("/projects/demo", "/projects", "/projects/demo/agents"):
        assert "ghp_supersecret" not in client.get(path, headers=AUTH).text


def test_git_credentials_reach_the_subprocess_only_when_push_is_allowed(client, root: Path):
    new_project(client, root)
    client.post("/projects/demo/agents", json={"name": "dev"}, headers=AUTH)
    svc = client.app.state.cc.projects

    client.put("/projects/demo/settings",
               json={"github_token": "ghp_abc", "git_user_name": "bot"}, headers=AUTH)
    env = svc.agent_env("demo__dev")
    assert env["GIT_AUTHOR_NAME"] == "bot"
    assert "GH_TOKEN" not in env, "no push means no credential"

    client.put("/projects/demo/settings", json={"allow_push": True}, headers=AUTH)
    env = svc.agent_env("demo__dev")
    assert env["GH_TOKEN"] == "ghp_abc"
    assert env["GIT_TERMINAL_PROMPT"] == "0"          # never hang on a prompt
    assert "credential.https://github.com.helper" in env["GIT_CONFIG_KEY_0"]

    client.put("/projects/demo/settings", json={"github_token": ""}, headers=AUTH)
    assert "GH_TOKEN" not in svc.agent_env("demo__dev")
    assert client.get("/projects/demo/settings", headers=AUTH).json()["github_token_set"] is False


def test_an_agent_outside_a_project_gets_no_credentials(client, root: Path):
    new_project(client, root)
    client.put("/projects/demo/settings",
               json={"github_token": "ghp_abc", "allow_push": True}, headers=AUTH)
    # `alpha` comes from agents.yaml, so it belongs to no project
    assert client.app.state.cc.projects.agent_env("alpha") == {}


def test_secrets_go_with_the_project(client, root: Path):
    new_project(client, root)
    client.put("/projects/demo/settings", json={"github_token": "ghp_abc"}, headers=AUTH)
    store = client.app.state.cc.store
    assert store.get_secret("demo", "github_token") == "ghp_abc"
    client.delete("/projects/demo", headers=AUTH)
    assert store.get_secret("demo", "github_token") is None


def test_creating_a_project_can_import_its_milestones(client, root: Path):
    docs = root / "docs"
    docs.mkdir()
    (docs / "auth.md").write_text("# Ship authentication\n\nOAuth first.\n")
    (docs / "api.md").write_text("# Ship the API\n")

    body = client.post(
        "/projects",
        json={"name": "Auto", "root_dir": str(root), "import_milestones": True},
        headers=AUTH,
    ).json()
    assert body["milestones"] == 2
    assert len(body["imported"]["imported"]) == 2
    assert {m["title"] for m in client.get("/projects/auto/milestones", headers=AUTH).json()} == {
        "Ship authentication", "Ship the API",
    }


def test_import_at_creation_is_opt_in(client, root: Path):
    (root / "docs").mkdir()
    (root / "docs" / "a.md").write_text("# A goal\n")
    body = new_project(client, root)
    assert body["milestones"] == 0 and body["imported"] is None


def test_a_missing_docs_directory_does_not_undo_the_project(client, root: Path):
    """The project is otherwise exactly right; report it, do not refuse it."""
    body = client.post(
        "/projects",
        json={"name": "Nodocs", "root_dir": str(root), "import_milestones": True},
        headers=AUTH,
    ).json()
    assert body["id"] == "nodocs"
    assert body["milestones"] == 0
    assert "nothing imported" in body["imported"]["detail"]
    assert client.get("/projects/nodocs", headers=AUTH).status_code == 200


def test_creation_import_honours_explicit_paths(client, root: Path):
    (root / "plan.md").write_text("# From a named file\n")
    body = client.post(
        "/projects",
        json={"name": "Named", "root_dir": str(root),
              "import_milestones": True, "import_paths": ["plan.md"]},
        headers=AUTH,
    ).json()
    assert [i["title"] for i in body["imported"]["imported"]] == ["From a named file"]
