# Backend API manual

Reference for every HTTP endpoint the server exposes. Generated against the
running app and kept in step with `server/main.py`.

- **Base URL** — `http://127.0.0.1:8787` by default (`CC_AUTOMATION_PORT` moves it)
- **Specs** — `README.md` (messages, agents, logs) and `docs/PROJECTS.md` (projects, tasks, planning)
- **Interactive** — FastAPI serves `/docs` and `/openapi.json`; both need the API key

---

## Contents

1. [Authentication](#1-authentication)
2. [Errors](#2-errors)
3. [Conventions](#3-conventions)
4. [Health](#4-health)
5. [Agents](#5-agents)
6. [Messages](#6-messages)
7. [Projects](#7-projects)
8. [Project agents](#8-project-agents)
9. [Tasks](#9-tasks)
10. [Planning](#10-planning)
11. [Logs](#11-logs)
12. [Console](#12-console)
13. [Endpoint index](#13-endpoint-index)

---

## 1. Authentication

Every request carries a single static key in the `X-API-Key` header. The
comparison is constant-time (`hmac.compare_digest`); a mismatch is `401`.

```bash
KEY=$(cat ~/.cc-automation/api_key)
curl -H "X-API-Key: $KEY" localhost:8787/agents
```

The server resolves the key at startup from `CC_AUTOMATION_API_KEY`, else from
`~/.cc-automation/api_key` (mode `600`). If neither exists, it refuses to boot.
There is no rotation mechanism: replace the stored string and restart.

**Unauthenticated paths** — `GET /health`, `GET /`, and `/web/*`. The console's
static files are open because a browser cannot put a header on a navigation;
they contain no secrets and every call the console then makes carries the key.

---

## 2. Errors

Errors are a JSON object with a `detail` field.

```json
{ "detail": "unknown project 'ghost'" }
```

| Status | Meaning |
|--------|---------|
| `400` | Malformed path parameter (bad date or topic slug), or a non-UTF-8 body |
| `401` | Missing or wrong `X-API-Key` |
| `404` | No such project, agent, task, message, or log |
| `409` | A conflict with current state — duplicate name, second projectmanager, deleting something with a running task |
| `422` | The request body or a routing tag failed validation |
| `502` | The projectmanager ran but failed, or its reply was not a usable plan |
| `504` | The projectmanager did not answer within its `timeout_s` + 60s |

Two `detail` shapes are not plain strings:

**Tag routing failure** (`POST /messages`, 422):

```json
{ "detail": { "error": "no agent matched tags ['ghost']; known agents: ['alpha']",
              "unmatched_tags": ["ghost"] } }
```

**Request body validation** (FastAPI's own, 422) — a list of `{loc, msg, type}`:

```json
{ "detail": [ { "loc": ["body", "root_dir"], "msg": "Field required",
                "type": "missing" } ] }
```

---

## 3. Conventions

**Timestamps** are UTC ISO-8601 with an offset: `2026-08-07T08:17:05.739524Z`.

**Ids** are 16 hex characters (`message_id`, task `id`). Project ids are slugs
derived from the project name: lowercased, non-alphanumerics collapsed to `-`,
capped at 32 characters. `"Demo Proj"` becomes `demo-proj`.

**Runtime agent names.** A project agent is addressed inside its project by its
short name (`dev`), but the dispatcher, `state/sessions.json` and the log
entries all see `{project_id}__{agent_name}` (`demo__dev`). Both appear in
`GET /projects/{pid}/agents` as `name` and `runtime_name`.

**Message dispatch is fire-and-forget.** `POST /messages` and `POST
/projects/{pid}/tasks` return as soon as the work is queued. Results arrive in
the Markdown logs and, for tasks, on the task row. `POST /projects/{pid}/plan`
is the one endpoint that blocks until its run finishes.

**Queue semantics.** One FIFO queue and one worker per agent. Two messages to
the same agent run serially, in order; different agents run in parallel. This is
mandatory — two concurrent `--resume` runs against one session would corrupt it.

**Durability.** Projects, project agents and tasks are in the project store and
survive a restart. Plain `/messages` queues are in memory and do not (README §6).
The store is KDS by default and SQLite on request — see `docs/kds-backend.md`
and the `store` field of `GET /health`.

---

## 4. Health

### `GET /health`

The only endpoint that needs no key. Use it for liveness checks.

```json
{ "status": "ok", "store": "kds" }
```

`store` is the backend actually in use — `kds` or `sqlite`. It is reported
because the KDS URL may declare `?fallback=sqlite`, and this is the only way to
see that the fallback fired. The two databases do not sync.

---

## 5. Agents

### `GET /agents`

Every agent the dispatcher can route to — those from `agents.yaml` and those
owned by projects, in one list.

```bash
curl -H "X-API-Key: $KEY" localhost:8787/agents
```

```json
[
  { "name": "researcher", "tags": ["research", "read"],
    "cwd": "/home/ec2-user/workspaces/researcher",
    "session_id": "741ba434-f58c-446a-8bf9-df5623769e65",
    "queue_depth": 0, "busy": false, "project": null, "role": null },
  { "name": "demo__pm", "tags": [], "cwd": "/home/ec2-user/workspaces/demo",
    "session_id": null, "queue_depth": 2, "busy": true,
    "project": "demo", "role": "manager" }
]
```

| Field | Type | Notes |
|-------|------|-------|
| `name` | string | The runtime name; for project agents, `{pid}__{agent}` |
| `tags` | string[] | Routing tags from `agents.yaml`. Project agents carry none here; they are reachable via `project:{pid}` |
| `cwd` | string | Directory the `claude` subprocess runs in |
| `session_id` | string \| null | Current Claude Code session, `null` until the first run |
| `queue_depth` | int | Messages waiting, not counting the one in flight |
| `busy` | bool | Whether a run is in flight right now |
| `project` | string \| null | Owning project, `null` for `agents.yaml` agents |
| `role` | `"manager"` \| `"worker"` \| null | `null` for `agents.yaml` agents |

Agents in `agents.yaml` are read once at startup. Editing that file means
restarting; project agents are the ones that can be created at runtime.

---

## 6. Messages

The original, project-free path: push text at one or more agents by tag.

### `POST /messages` → `202`

```bash
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text":"summarise today","tags":["research"],"topic":"daily"}' \
  localhost:8787/messages
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `text` | string | yes | Passed to `claude -p` as an argv element, never through a shell |
| `tags` | string[] | yes | At least one non-blank tag |
| `topic` | string | no | Log file the entry lands in; sanitised to `[a-z0-9-]` |

**Tag resolution, in priority order:**

1. `session:<id>` — dispatch to whichever agent currently holds that session id.
   Wins outright over every other tag in the request.
2. `agent:<name>`, or a bare tag matching an agent's name or tag list — every
   match receives the message. `project:<pid>` matches every agent in a project.
3. `global` — fan out to every registered agent.

If no tag resolves, the response is `422` and the message is dropped — never
silently.

**Topic fallback.** With no `topic`, a single-target message is logged under the
agent's name; a fan-out is logged under `global`, so one message stays in one
file rather than scattering across several.

```json
{ "message_id": "3f2a91c04b7d8e15",
  "targets": ["researcher"],
  "topic": "daily" }
```

### `GET /messages/{message_id}` → `200`

```json
{ "message_id": "3f2a91c04b7d8e15", "status": "running", "topic": "daily",
  "tags": ["research"],
  "targets": [
    { "agent": "researcher", "status": "running",
      "started_at": "2026-08-07T08:20:11Z", "finished_at": null, "error": null }
  ],
  "created_at": "2026-08-07T08:20:10Z" }
```

`status` is `queued | running | done | failed`, aggregated across targets: `done`
only when all targets succeeded, `failed` when all finished and at least one
failed, otherwise `running` or `queued`.

This map is **in memory and bounded to 5000 entries**. A restart clears it and
old entries are evicted; `404` here does not mean the message never ran. The
logs are the durable record.

---

## 7. Projects

A project owns a root directory, an `overview.md` brief, its agents, and its
tasks. At most one of its agents is the projectmanager.

### `POST /projects` → `201`

```bash
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{
  "name": "Demo Proj",
  "root_dir": "/home/ec2-user/workspaces/demo",
  "tool_policy": ["Read", "Glob", "Grep", "Edit", "Write"],
  "manager": {"name": "pm", "role": "manager",
              "allowed_tools": ["Read", "Glob", "Grep"]}
}' localhost:8787/projects
```

| Field | Type | Required | Notes |
|-------|------|----------|-------|
| `name` | string | yes | The id is slugified from this and must be unique |
| `root_dir` | string | yes | Must already exist. Every agent's `cwd` must resolve inside it |
| `tool_policy` | string[] | no | Defaults to `["Read","Glob","Grep","Edit","Write"]`. Must not be empty |
| `manager` | object | no | A [project agent](#8-project-agents) body; `role` is forced to `manager` |

**`tool_policy` is the operator's one lever.** It caps what any agent in this
project can be granted, the projectmanager included, and **cannot be widened
later** — changing it means recreating the project. It matters because
`permission_mode` never prompts (`claude -p` has nobody to ask), so the tool list
is the real containment boundary. Matching is on the base name: `Bash` in the
policy permits an agent to hold `Bash(ls *)`.

If `manager` is supplied and fails validation, the project is deleted again and
the error returned — no half-built project is left behind.

Errors: `409` id already taken · `422` `root_dir` missing, empty `tool_policy`,
name with no usable characters, or an invalid manager.

### `GET /projects` → `200`
### `GET /projects/{pid}` → `200`

```json
{ "id": "demo-proj", "name": "Demo Proj",
  "root_dir": "/home/ec2-user/workspaces/demo",
  "tool_policy": ["Read", "Glob", "Grep", "Edit", "Write"],
  "created_at": "2026-08-07T08:17:05.739524Z",
  "agents": ["dev", "pm"], "manager": "pm", "open_tasks": 1 }
```

`agents` holds short names. `open_tasks` counts `queued` plus `running`.

### `DELETE /projects/{pid}` → `200`

```json
{ "deleted": "demo-proj" }
```

Removes the project's agents from the pool and its rows from the store. **Files
under `root_dir` are never touched.**

Refused with `409` while any of its tasks is `running`: cancelling a worker
mid-run abandons its `claude` subprocess rather than killing it, so the wait is
deliberate. `timeout_s` is the backstop.

### `GET /projects/{pid}/overview` → `200` `text/markdown`

Returns `{root_dir}/overview.md` verbatim — the brief the projectmanager plans
from. `404` if the file does not exist yet.

### `PUT /projects/{pid}/overview` → `200`

The **raw request body** is written to the file; there is no JSON envelope. Any
`Content-Type` is accepted, and the body must be UTF-8 (`400` otherwise).

```bash
curl -H "X-API-Key: $KEY" -X PUT --data-binary @overview.md \
  localhost:8787/projects/demo-proj/overview
```

```json
{ "path": "/home/ec2-user/workspaces/demo/overview.md", "bytes": 214 }
```

---

## 8. Project agents

### `GET /projects/{pid}/agents` → `200`

```json
[ { "project_id": "demo-proj", "name": "dev", "runtime_name": "demo-proj__dev",
    "role": "worker",
    "config": { "name": "demo-proj__dev", "tags": [],
                "cwd": "/home/ec2-user/workspaces/demo/services/api",
                "system_prompt": "You build the HTTP API.",
                "allowed_tools": ["Read", "Edit", "Write"],
                "permission_mode": "bypassPermissions",
                "max_budget_usd": 0.5, "timeout_s": 900 },
    "created_at": "2026-08-07T08:17:05.766Z" } ]
```

### `POST /projects/{pid}/agents` → `201`

```bash
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{
  "name": "dev", "cwd": "services/api",
  "allowed_tools": ["Read", "Edit", "Write"],
  "system_prompt": "You build and test the HTTP API."
}' localhost:8787/projects/demo-proj/agents
```

| Field | Type | Default | Notes |
|-------|------|---------|-------|
| `name` | string | — | `[A-Za-z0-9_-]{1,64}`, unique in the project, may not contain `__` |
| `role` | `manager`\|`worker` | `worker` | At most one `manager` per project |
| `system_prompt` | string \| null | `null` | **Appended** to Claude Code's own prompt, not substituted |
| `allowed_tools` | string[] | `[]` | Empty inherits the project `tool_policy` |
| `permission_mode` | string | `bypassPermissions` | One of `acceptEdits, auto, bypassPermissions, manual, dontAsk, plan` |
| `cwd` | string \| null | `null` | Relative to `root_dir`; `null` means the root itself |
| `max_budget_usd` | float | `0.50` | Per-invocation spend cap (`--max-budget-usd`) |
| `timeout_s` | int | `900` | Wall-clock kill for one invocation |

**An empty `allowed_tools` inherits the policy, it does not mean "no tools".**
Passing no `--allowedTools` to the CLI would mean *every* tool, so project agents
fall back to `tool_policy` rather than to unrestricted.

**Never choose a `permission_mode` that can prompt.** `claude -p` has nobody to
answer, so a prompt hangs the worker until `timeout_s`.

The agent gets its queue, worker and session immediately — no restart.

Errors: `409` name taken in the project, runtime name taken globally, a second
manager, or 12 agents already · `422` tools outside `tool_policy`, a `cwd`
outside `root_dir` or that does not exist, or an invalid name.

### `DELETE /projects/{pid}/agents/{name}` → `200`

```json
{ "deleted": "dev", "tasks_cancelled": 2 }
```

Stops the worker and drops the registry entry. Its `queued` tasks are cancelled.
Refused with `409` while one of its tasks is `running`.

---

## 9. Tasks

A task is one instruction assigned to one project agent. Running it is an
ordinary message: it produces a log entry under topic `{project_id}`, and its
result, cost and error land back on the task row.

**Lifecycle:** `queued → running → done | failed`, or `queued → cancelled`.

### `POST /projects/{pid}/tasks` → `201`

```bash
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"agent":"dev","title":"Add ping","text":"Add POST /ping with a test."}' \
  localhost:8787/projects/demo-proj/tasks
```

| Field | Type | Notes |
|-------|------|-------|
| `agent` | string | Short name within the project |
| `title` | string | Non-empty; a label for the list views |
| `text` | string | Non-empty. **The entire context the agent receives** — it has no other |

The agent is sent `[task {id}] {title}\n\n{text}`. Enqueued immediately.

Errors: `404` no such agent in this project.

### `GET /projects/{pid}/tasks` → `200`

Query: `status` — one of `queued, running, done, failed, cancelled`.

### `GET /tasks` → `200`

The cross-project list. This is what the store is for.

| Query | Default | Notes |
|-------|---------|-------|
| `status` | all | One status |
| `project` | all | A project id |
| `agent` | all | A **runtime** name (`demo-proj__dev`) |
| `limit` | `200` | Capped at 1000 |

```bash
curl -H "X-API-Key: $KEY" 'localhost:8787/tasks?status=failed'
```

Newest first, by `created_at`.

### `GET /tasks/{tid}` → `200`

```json
{ "id": "a4c1f0e29b3d7c68", "project_id": "demo-proj", "agent": "demo-proj__dev",
  "title": "Add ping", "text": "Add POST /ping with a test.",
  "status": "done", "created_by": "api", "message_id": "9d0b1e77aa32c541",
  "result": "Added the handler and a test; both pass.",
  "error": null, "cost_usd": 0.0413,
  "created_at": "2026-08-07T08:20:10Z",
  "started_at": "2026-08-07T08:20:10Z",
  "finished_at": "2026-08-07T08:21:44Z" }
```

`created_by` is `"api"` or `"manager"`. `result` is set on success and `error` on
failure; never both.

### `POST /tasks/{tid}/cancel` → `200`

Returns the updated task. Only a `queued` task can be cancelled — `409`
otherwise. Cancellation is checked when the worker picks the job up, so nothing
is spent; a task already inside `claude` runs to completion or `timeout_s`.

### Restart behaviour

On startup, `queued` tasks are re-enqueued and `running` tasks are marked
`failed` with `"server restarted mid-run"` — their subprocess died with the
server and the result is unrecoverable. A queued task whose agent no longer
exists becomes `cancelled`.

---

## 10. Planning

### `POST /projects/{pid}/plan` → `200`

The projectmanager reads `overview.md` and the current state, then answers with
a JSON plan. **The manager plans; the server acts** — it never calls this API,
holds no key, and can do exactly what the action vocabulary below allows.

```bash
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"note":"focus on the API this round"}' \
  localhost:8787/projects/demo-proj/plan
```

Body: `{"note": string | null}` — optional, appended to the prompt.

**This endpoint blocks** for the length of the manager's run. It goes through the
manager's own queue rather than around it, so two overlapping plan requests
cannot resume one session concurrently. Set your client timeout above the
manager's `timeout_s` (default 900s).

**Nothing replans on its own.** No timer, no cascade on task completion. Chain it
yourself if you want a loop.

The manager's prompt contains: `overview.md`, the agent roster, open tasks, the
last 10 finished tasks with their results, the `tool_policy`, the action schema,
and your note.

**Action vocabulary** — the manager may emit only these:

| `op` | Fields | Notes |
|------|--------|-------|
| `create_agent` | `name`, `system_prompt?`, `allowed_tools?`, `cwd?`, `max_budget_usd?`, `timeout_s?` | Always a worker; `role` is not in the schema, so a manager cannot be appointed |
| `delete_agent` | `name` | Refused for the manager itself |
| `create_task` | `agent`, `title`, `text` | |
| `cancel_task` | `task_id` | Must belong to this project |
| `note` | `text` | Recorded, not acted on |

Server-enforced limits, each producing a rejection rather than an error: at most
25 actions per plan and 12 agents per project; `allowed_tools ⊆ tool_policy`;
`cwd` inside `root_dir`. Actions apply **in order**, so a `create_task` may name
an agent a `create_agent` created earlier in the same plan.

```json
{ "project_id": "demo-proj", "message_id": "5c81d0af62e39b74",
  "summary": "Start the API and its tests.",
  "applied": [
    { "op": "create_agent", "name": "api-dev",
      "allowed_tools": ["Read", "Edit", "Write"], "cwd": "services/api" },
    { "op": "create_task", "agent": "api-dev", "title": "Scaffold",
      "text": "Create the app module." }
  ],
  "rejected": [
    { "action": { "op": "create_agent", "name": "shell", "allowed_tools": ["Bash"] },
      "reason": "tools outside the project tool_policy: ['Bash']; policy is [...]" }
  ],
  "tasks_created": ["a4c1f0e29b3d7c68"],
  "raw_reply": "```json\n{ ... }\n```" }
```

**A rejected action never fails the round.** The manager is a language model; a
half-usable plan half-applies and the rejections are reported. Each action is
validated on its own, so one malformed entry does not discard the rest. Always
read `rejected` — a `200` does not mean everything was applied.

The reply parser tolerates fenced code blocks and surrounding prose; it extracts
the outermost JSON object.

Errors: `409` the project has no projectmanager · `422` the reply contained no
usable plan · `502` the manager's run failed · `504` no answer within
`timeout_s + 60`.

---

## 11. Logs

Markdown work logs at `logs/{YYYY-MM-DD}/{topic}.md`. **The runner writes them,
not the agent** — each entry is derived from the JSON result of a run, so a line
exists whether or not Claude cooperated. Writes are a single append under a
per-file lock, so concurrent entries never interleave.

Task runs log under topic `{project_id}`; planning rounds under
`{project_id}-plan`.

### `GET /logs` → `200`

```json
{ "dates": ["2026-08-06", "2026-08-07"] }
```

### `GET /logs/{date}` → `200`

`date` must be exactly `YYYY-MM-DD` — `2026-8-7` is rejected with `400`.

```json
{ "date": "2026-08-07", "topics": ["demo-proj", "demo-proj-plan", "daily"] }
```

### `GET /logs/{date}/{topic}` → `200` `text/markdown`

```markdown
## [08:20:10] agent: demo-proj__dev | message: 9d0b1e77aa32c541

**Input:** [task a4c1f0e2] Add ping

Add POST /ping with a test.

**Result:**
Added the handler and a test; both pass.

**Meta:** duration=94.2s, cost_usd=0.0413, status=ok

---
```

`date` and `topic` are validated on the read path with the same functions used on
the write path, and the resolved path is checked for containment under `logs/`.
Traversal attempts return `400` or `404` and never read outside the log root.

---

## 12. Console

`GET /` redirects to `/web/`, the browser UI. See [`console.md`](console.md).

---

## 13. Endpoint index

| Method | Path | Success | Notes |
|--------|------|---------|-------|
| `GET` | `/health` | 200 | No key needed |
| `GET` | `/agents` | 200 | Static and project agents |
| `POST` | `/messages` | 202 | Fire-and-forget, tag-routed |
| `GET` | `/messages/{message_id}` | 200 | In-memory, bounded |
| `GET` | `/logs` | 200 | Dates |
| `GET` | `/logs/{date}` | 200 | Topics |
| `GET` | `/logs/{date}/{topic}` | 200 | Raw Markdown |
| `POST` | `/projects` | 201 | Optionally creates the manager |
| `GET` | `/projects` | 200 | |
| `GET` | `/projects/{pid}` | 200 | |
| `DELETE` | `/projects/{pid}` | 200 | 409 while a task runs |
| `GET` | `/projects/{pid}/overview` | 200 | `text/markdown` |
| `PUT` | `/projects/{pid}/overview` | 200 | Raw body |
| `GET` | `/projects/{pid}/agents` | 200 | |
| `POST` | `/projects/{pid}/agents` | 201 | Live, no restart |
| `DELETE` | `/projects/{pid}/agents/{name}` | 200 | Cancels its queued tasks |
| `GET` | `/projects/{pid}/tasks` | 200 | `?status=` |
| `POST` | `/projects/{pid}/tasks` | 201 | Enqueued immediately |
| `POST` | `/projects/{pid}/plan` | 200 | **Blocks** for the manager's run |
| `GET` | `/tasks` | 200 | Cross-project; `?status=&project=&agent=&limit=` |
| `GET` | `/tasks/{tid}` | 200 | With result and cost |
| `POST` | `/tasks/{tid}/cancel` | 200 | Queued only |
| `GET` | `/` | 307 | → `/web/` |

---

## Worked example

```bash
KEY=$(cat ~/.cc-automation/api_key); H="X-API-Key: $KEY"
J='Content-Type: application/json'

mkdir -p ~/workspaces/demo
printf '# Demo\n\nShip a ping endpoint with tests.\n' > ~/workspaces/demo/overview.md

# 1. project + projectmanager
curl -s -H "$H" -H "$J" -d '{
  "name":"Demo","root_dir":"'"$HOME"'/workspaces/demo",
  "manager":{"name":"pm","role":"manager","allowed_tools":["Read","Glob","Grep"]}}' \
  localhost:8787/projects

# 2. let the manager staff and plan the project
curl -s -H "$H" -H "$J" -d '{}' localhost:8787/projects/demo/plan

# 3. watch the work
curl -s -H "$H" 'localhost:8787/tasks?project=demo'
curl -s -H "$H" "localhost:8787/logs/$(date +%F)/demo"
```
