# Work Instruction: Projects, Project Agents, and Tasks

**Date:** 2026-08-07
**Status:** Draft v1 (extends `README.md`, the v1 server spec)
**Owner:** Louis

---

## 1. Purpose

Add a project layer on top of the message/agent server, so one API can drive
work across several codebases at once. A **project** owns a brief
(`overview.md`), a set of **agents**, and a queue of **tasks**. Exactly one of
a project's agents is the **projectmanager**: it reads the brief and the current
state and answers with a plan — which agents to create or remove, and which
tasks to hand out.

This is the v2 feature README §10 defers ("dynamic agent creation/deletion via
API"). It supersedes that line.

## 2. Design decisions

Three choices shape everything below.

**The manager plans; the server acts.** The projectmanager never calls the API.
It is invoked with the project state as a prompt and must answer with a JSON
plan. The server validates every action against project policy and applies the
ones that pass. The API key is never handed to a subprocess, the manager's
authority is exactly the action vocabulary in FR-5, and a planning round is
reproducible from its input.

**State lives in SQLite** (`state/projects.db`). README §6 lets the message
queue die with the process. Task rows do not get that licence: an assigned task
is a commitment, so tasks, agents and projects are durable and queued tasks are
re-enqueued at startup.

**Planning is explicitly triggered.** `POST /projects/{id}/plan` is the only
thing that runs the manager. Nothing replans on its own — no timer, no cascade
on task completion. Automation is the caller's to compose, and an idle project
costs nothing.

## 3. Architecture delta

```
API Server
    ├── Agent Registry        — agents.yaml (static) + project agents (SQLite)
    ├── Agent Pool      [NEW] — add/remove an agent's registry entry, queue and
    │                           worker together, at runtime
    ├── Dispatcher            — unchanged rules; queues now come and go
    ├── Agent Runner          — unchanged; a Job may now carry a task_id and a
    │                           future, so a caller can await one run
    ├── Project Store   [NEW] — SQLite: projects, agents, tasks
    ├── Project Service [NEW] — project/agent/task rules, task dispatch
    └── Planner         [NEW] — build the manager prompt, parse and apply plans
```

A project agent is an ordinary agent to everything below the pool. It gets a
queue, a serial worker, and its own resumable Claude Code session, exactly like
an `agents.yaml` agent.

### Naming

Agent names are unique **within** a project. The runtime name the registry,
dispatcher and `state/sessions.json` see is `{project_id}__{agent_name}`.
Creation is refused if that runtime name is already taken by a static agent or
another project. `project_id` is a slug derived from the project name.

## 4. Functional Requirements

### FR-P1: Projects
- `POST /projects` accepts `{ name, root_dir, tool_policy?, manager? }` → `201`.
  - `root_dir` must be an existing directory. It is the project's boundary:
    every project agent's `cwd` must resolve inside it.
  - `tool_policy` is the set of tools project agents may be given. Defaults to
    `[Read, Glob, Grep, Edit, Write]`. **The manager cannot widen it** — this is
    the operator's one lever over what its agents can do, and `permission_mode`
    is never allowed to prompt (README FR-3), so the tool list is the boundary.
  - `manager` optionally creates the projectmanager in the same call; otherwise
    create it later with `role: manager`.
- `GET /projects`, `GET /projects/{pid}`, `DELETE /projects/{pid}`.
  - Delete removes the project's agents from the pool and its rows from the
    store. Files under `root_dir` are never touched.
- `GET|PUT /projects/{pid}/overview` reads and writes `{root_dir}/overview.md`
  as `text/markdown`. The manager is prompted with this file verbatim.

### FR-P2: Project agents
- `POST /projects/{pid}/agents` accepts an `agents.yaml`-shaped body plus
  `role: manager|worker` (default `worker`).
- **At most one manager per project**, enforced by a partial unique index in the
  schema, not only in application code.
- `allowed_tools` must be a subset of the project's `tool_policy`; `cwd` is
  relative to `root_dir` and must stay inside it. Violations are `422`.
- `DELETE /projects/{pid}/agents/{name}` stops the worker and drops the registry
  entry. Refused (`409`) while the agent has a running task. Queued tasks for
  that agent are cancelled.
- Project agents are also routable from `POST /messages`, via the implicit tags
  `project:{pid}` (fan-out) and `agent:{pid}__{name}`. `project:` joins
  `session:` and `agent:` as a prefix `agents.yaml` may not claim.

### FR-P3: Tasks
- `POST /projects/{pid}/tasks` accepts `{ agent, title, text }` → `201`. The
  task is enqueued on that agent immediately.
- Status: `queued → running → done | failed`, or `cancelled` from `queued`.
- A task's run is an ordinary message: it produces a log entry under topic
  `{pid}` (README FR-4) and its result text, cost and error are written back to
  the task row.
- `POST /tasks/{tid}/cancel` — only from `queued`. A running task is not
  interrupted; `timeout_s` is the only hard stop.
- `GET /tasks?status=&project=` queries **across projects**, which is the point
  of the store. `GET /tasks/{tid}` returns one task with its result.
- On startup, tasks left `queued` are re-enqueued; tasks left `running` are
  marked `failed` with "server restarted mid-run" — the subprocess died with the
  server and its result is unrecoverable.

### FR-P4: Planning round
`POST /projects/{pid}/plan` (optional body `{ note }`, appended to the prompt):

1. Fail `409` if the project has no manager.
2. Build the prompt: `overview.md`, the current agent roster, open and recently
   finished tasks, the project's `tool_policy`, the action schema, and the
   instruction to answer with JSON only.
3. Enqueue it on the manager's own queue and await the result. Going through the
   queue rather than around it keeps README FR-3's rule intact: the manager's
   session is never resumed concurrently, even if two plan requests overlap.
4. Parse the reply (see FR-P5), validate each action, apply the valid ones in
   one transaction, enqueue any created tasks.
5. Respond `200` with `{ applied: [...], rejected: [{action, reason}], summary,
   tasks_created: [...] }`.

A rejected action never fails the round. The manager is a language model; a plan
that is half-usable should half-apply, with the rejections reported rather than
swallowed.

### FR-P5: Plan protocol
The manager must answer with one JSON object. A fenced code block or surrounding
prose is tolerated — the parser extracts the outermost JSON object — because
models add it and failing the round over punctuation is not useful.

```json
{
  "summary": "why this plan",
  "actions": [
    {"op": "create_agent", "name": "api-dev", "system_prompt": "...",
     "allowed_tools": ["Read", "Edit", "Write"], "cwd": "services/api"},
    {"op": "delete_agent", "name": "scratch"},
    {"op": "create_task", "agent": "api-dev", "title": "...", "text": "..."},
    {"op": "cancel_task", "task_id": "..."},
    {"op": "note", "text": "free-text observation, recorded only"}
  ]
}
```

Server-enforced limits, each producing a rejection rather than an error:
- ≤ 25 actions per plan; ≤ 12 agents per project.
- `allowed_tools ⊆ tool_policy`; `cwd` inside `root_dir`.
- The manager may not create, delete, or assign tasks to a `manager` agent.
- `create_task` must name an agent that exists after the plan's own
  `create_agent` actions are applied — actions are applied in order.

## 5. Storage

> **Superseded:** `docs/kds-backend.md` moves this to KDS, keeping the schema's
> meaning and the same `ProjectStore` interface. SQLite remains the fallback
> and the backend the tests run on.

`state/projects.db`, one connection guarded by a lock (write volume is a handful
of rows per planning round; a connection pool would be ceremony).

```sql
projects(id PK, name, root_dir, tool_policy JSON, created_at)
agents(id PK, project_id FK, name, runtime_name UNIQUE, role, config JSON,
       created_at, UNIQUE(project_id, name))
CREATE UNIQUE INDEX ... ON agents(project_id) WHERE role = 'manager';
tasks(id PK, project_id FK, agent, title, text, status, created_by,
      message_id, result, error, cost_usd, created_at, started_at, finished_at)
```

## 6. Acceptance criteria

- [ ] Creating a second manager in one project is rejected, by the database.
- [ ] A project agent created via API gets a queue and a worker without a
      restart, and shows up in `GET /agents`.
- [ ] A task posted to a project agent runs, lands in `logs/{date}/{pid}.md`,
      and its result text is readable from `GET /tasks/{tid}`.
- [ ] A plan whose manager reply is wrapped in a ```json fence still applies.
- [ ] A plan action requesting a tool outside `tool_policy` is rejected, the
      round still returns `200`, and the other actions apply.
- [ ] `create_agent` with `cwd: "../.."` is rejected.
- [ ] After a restart, `queued` tasks run and `running` tasks are `failed`.
- [ ] `GET /tasks?status=failed` returns failures from every project.

## 7. Out of scope

- Automatic replanning (on task completion, file change, or timer).
- Inter-agent messaging; agents communicate only through the manager's plans.
- Per-project API keys or any multi-tenant separation.
- Rolling a plan back once applied.
