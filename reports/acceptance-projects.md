# Acceptance evidence: `docs/PROJECTS.md` §6

Read-only audit. Each criterion below is mapped to the test that actually
executes the described behaviour; test bodies and the implementation they drive
were read, not just test names. Order matches `docs/PROJECTS.md` §6.

---

## 1. Second manager rejected by the database

> Creating a second manager in one project is rejected, by the database.

**Verdict: COVERED**

`tests/test_store_contract.py:171` — `test_one_projectmanager_per_project`.
The second `store.add_agent("demo", "pm2", "manager", ...)` must raise
`StoreError` matching `"already has a projectmanager"`, and the following
`add_agent(..., "worker", ...)` must still succeed, so the rejection is
specific to the manager role rather than to a duplicate insert.

The "by the database" part holds for the SQLite backend: the schema declares
`CREATE UNIQUE INDEX one_manager_per_project ON agents(project_id) WHERE role
= 'manager'` (`server/store/sqlite.py:60`), and `add_agent` raises only after
catching `sqlite3.IntegrityError` (`server/store/sqlite.py:304`) — the message
is reconstructed by `_explain_conflict` afterwards, so there is no Python
pre-check that could mask a missing index. Deleting the index would fail this
test.

Supporting, at the API level: `tests/test_projects.py:145`
`test_second_manager_is_refused` asserts `409` with `"projectmanager"` in the
detail; `server/projects.py:360` turns the `StoreError` into that `409`, so the
HTTP path is the same database constraint.

Note: the same contract test also runs against the KDS backend, where the rule
is enforced in Python rather than by an index. That is the documented design
(`server/store/kds.py`), and SQLite is the backend §5 names as the one the tests
run on, so the criterion is proven on the backend it is written about.

---

## 2. Project agent gets a queue and a worker without a restart

> A project agent created via API gets a queue and a worker without a
> restart, and shows up in `GET /agents`.

**Verdict: COVERED**

Three tests together carry the three clauses, all against a live `TestClient`
with `start_workers=True`:

- Queue: `tests/test_projects.py:132` —
  `test_project_agent_gets_a_queue_without_a_restart`. After `POST
  /projects/demo/agents`, `dispatcher.depth("demo__dev") == 0` succeeds, which
  it can only do if the queue exists (`Dispatcher.depth` raises `KeyError`
  otherwise).
- Worker: `tests/test_projects.py:274` —
  `test_task_runs_and_records_its_result`. The agent is created via the API on
  the already-running app, a task posted to it reaches `status == "done"` with
  `result == "wrote the handler"` and `cost_usd == 0.01`. A result can only
  appear if `AgentPool.start` (`server/pool.py:55`) actually created and
  scheduled the worker coroutine for the new agent — no restart occurs in the
  test.
- `GET /agents`: `tests/test_projects.py:1119` —
  `test_the_agents_list_carries_the_same_state`. `GET /agents` contains
  `demo__dev` with `activity == "idle"`; `tests/test_projects.py:119`
  additionally asserts `demo__pm` appears there with `project == "demo"`.

---

## 3. Task runs, logs under the project topic, result readable

> A task posted to a project agent runs, lands in `logs/{date}/{pid}.md`,
> and its result text is readable from `GET /tasks/{tid}`.

**Verdict: COVERED**

- `tests/test_projects.py:274` — `test_task_runs_and_records_its_result`. The
  proving assertion is `task["result"] == "wrote the handler"` on the body of
  `GET /tasks/{tid}` (fetched by the `wait_for` helper at
  `tests/test_projects.py:106`), together with `status == "done"` and both
  timestamps set.
- `tests/test_projects.py:296` —
  `test_task_writes_a_log_entry_under_the_project_topic`. After the task
  finishes, `GET /logs/{date}/demo` returns text containing `agent: demo__dev`
  and the task text `do it` — the topic is the project id, as FR-P3 requires.

Caveat, not a gap: the log location is asserted through the `/logs/{date}/{topic}`
route rather than by reading `logs/{date}/demo.md` off disk. That route resolves
to exactly that path via `LogStore.path_for` (`server/logstore.py:60`), whose
`{root}/{date}/{slug(topic)}.md` shape is itself covered by
`tests/test_logstore.py:46`.

---

## 4. Fenced JSON plan still applies

> A plan whose manager reply is wrapped in a ```json fence still applies.

**Verdict: COVERED**

`tests/test_projects.py:470` — `test_plan_creates_an_agent_and_a_task`. The
scripted manager reply is literally `"```json\n" + json.dumps(plan) + "\n```"`;
the round returns `200` and `len(body["applied"]) == 3` with
`body["rejected"] == []`, so the fence was stripped and every action inside it
was applied. Follow-up assertions confirm the effects are real, not just
reported: `GET /projects/demo/agents` contains `api-dev`, and the created task
reaches `done`.

Unit-level support: `tests/test_projects.py:459`
`test_extract_json_object_tolerates_fences_and_prose` exercises
`extract_json_object` directly against fences, leading prose, decoy braces and
an embedded `}` inside a string.

---

## 5. Out-of-policy tool rejected, round still 200, rest applies

> A plan action requesting a tool outside `tool_policy` is rejected, the
> round still returns `200`, and the other actions apply.

**Verdict: COVERED**

`tests/test_projects.py:518` — `test_plan_rejects_bad_actions_and_applies_the_rest`.
All three clauses are asserted in one test: `resp.status_code == 200`;
`"tool_policy" in reasons` where `reasons` joins the rejection reasons (the
`bad-tools` action asked for `Bash`, outside the default policy); and
`[a["name"] for a in body["applied"]] == ["ok-dev"]`, so the one valid action
was applied alongside five rejections. The reason text originates at
`server/projects.py:325`, the same policy check the direct agent-creation route
uses.

---

## 6. `create_agent` with `cwd: "../.."` is rejected

> `create_agent` with `cwd: "../.."` is rejected.

**Verdict: COVERED**

- As a plan action: `tests/test_projects.py:518` —
  `test_plan_rejects_bad_actions_and_applies_the_rest`. The `bad-cwd` action
  uses `cwd: "../../etc"`, is absent from `applied`, and `"escapes"` appears in
  the joined rejection reasons.
- As a direct API call with the exact literal from the criterion:
  `tests/test_projects.py:181` — `test_cwd_cannot_escape_the_project_root`
  posts `{"name": "dev", "cwd": "../.."}` and asserts `422` with `"escapes"` in
  the detail.

Both paths run the same check — the planner's `create_agent` delegates to
`ProjectService.create_agent`, which resolves `root / spec.cwd` and refuses
anything not under the root (`server/projects.py:309`). The plan test uses
`"../../etc"` rather than the criterion's bare `"../.."`; since the check is a
resolved-path containment test with no special-casing of trailing components,
the two inputs take the identical branch, and the bare form is covered directly
by the API test above.

---

## 7. Restart re-enqueues `queued` and fails `running`

> After a restart, `queued` tasks run and `running` tasks are `failed`.

**Verdict: COVERED**

`tests/test_projects.py:434` —
`test_queued_tasks_survive_a_restart_and_running_ones_fail`. The first client
runs with `start_workers=False`, so the posted task genuinely stays `queued`
across the shutdown, and a second task is forced to `running` in the store.
After the client is torn down and a fresh app is built over the same home
directory, the test asserts `wait_for(c, queued)["status"] == "done"` (the
queued task was re-enqueued and actually ran) and that `tmid` is `failed` with
`"restarted"` in its error. It also asserts `demo__dev` is back in the registry
without touching `agents.yaml`, which is what makes the re-enqueue possible.

Store-level support: `tests/test_store_contract.py:289`
`test_fail_running_sweeps_only_running` proves `fail_running` touches `running`
rows and leaves `queued` ones alone.

---

## 8. `GET /tasks?status=failed` returns failures from every project

> `GET /tasks?status=failed` returns failures from every project.

**Verdict: PARTIAL**

`tests/test_projects.py:412` — `test_tasks_are_queryable_across_projects` is the
closest test. It creates two projects, runs one task in each, and asserts
`{t["project_id"] for t in done} == {"demo", "other"}` for `GET
/tasks?status=done`, plus that `GET /tasks?project=demo` returns exactly one
row. That proves the cross-project query and the `status` filter on the `/tasks`
route.

**Not proven:** no test issues `GET /tasks?status=failed` at all. Failures are
only ever observed one at a time through `GET /tasks/{tid}`
(`tests/test_projects.py:311`, `:434`), and never through the cross-project
list route. The store-level filter test (`tests/test_store_contract.py:302`)
also only filters on `"done"`. So the specific claim — that failures from more
than one project come back from this query — rests on inference from the
`done` case rather than on an assertion.

**Shortest test that would prove it:** the existing
`test_tasks_are_queryable_across_projects` body with `FakeWorker._spawn`
monkeypatched to return `RunResult(False, "boom")` (as
`test_failed_run_lands_on_the_task` already does), waiting for both tasks to
reach `failed`, then asserting that the `project_id` set of `GET
/tasks?status=failed` equals `{"demo", "other"}`.

---

## Summary

- **COVERED: 7**
- **PARTIAL: 1**
- **UNCOVERED: 0**

Criteria not fully proven:

- **PARTIAL — #8** `GET /tasks?status=failed` returns failures from every
  project: the cross-project `/tasks` query is proven only for `status=done`;
  no test ever requests `status=failed` on that route.
