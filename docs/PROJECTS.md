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

- `PATCH /projects/{pid}/agents/{name}` changes an agent's settings in place,
  keeping its **session, its queue and its worker**. This is the only sane way
  to retune one: the runtime name is what the session is filed under, so
  recreating an agent to change a budget risks stranding the conversation it
  depends on.
  - Patchable: `system_prompt`, `allowed_tools`, `permission_mode`, `model`,
    `max_budget_usd`, `timeout_s`. Omitted means "leave alone"; `""` clears
    `system_prompt` or `model`.
  - Not patchable: `name` and `role` — the first is what it is keyed by, the
    second a database constraint — and `cwd`, because every agent in a project
    works in the project's directory.
  - The new settings go through `build_config`, the same gate a new agent's do,
    so a patch cannot grant tools outside the project's `tool_policy`.
  - A run already in flight finishes on the settings it was spawned with; the
    next job off the queue uses the new ones.
- `PATCH /projects/{pid}` accepts `tool_policy`, the ceiling on what this
  project's agents may be granted. Widening it grants nothing retroactively —
  an agent's `allowed_tools` was resolved when it was created. Narrowing below
  what an existing agent already holds is refused (`409`, naming the agent and
  the tool): a policy its own agents violate is not the truth about the project.

### FR-P3: Tasks
- `POST /projects/{pid}/tasks` accepts `{ agent, title, text }` → `201`. The
  task is enqueued on that agent immediately.
- **Omitting `agent` files the task in the backlog** instead: the work is
  written down, given to nobody, and nothing is dispatched or spent. A backlog
  row has no `agent` and no `message_id`; every other status implies both.
- `POST /projects/{pid}/tasks/{tid}/assign` accepts `{ agent, branch?,
  milestone_id? }` and moves the row `backlog → queued` under that agent, which
  is what enqueues it. It is a compare-and-set on the status, so two callers
  racing to assign the same task cannot both enqueue it; the loser gets `409`.
- Status: `backlog → queued → running → done | failed`, or `cancelled` from
  either `backlog` or `queued`.
- A task's run is an ordinary message: it produces a log entry under topic
  `{pid}` (README FR-4) and its result text, cost and error are written back to
  the task row.
- `POST /tasks/{tid}/cancel` — only from `queued` or `backlog`. A running task
  is not interrupted; `timeout_s` is the only hard stop.
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
    {"op": "assign_task", "task_id": "...", "agent": "api-dev"},
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
- `assign_task` only moves a task that is still in the backlog. The prompt shows
  the manager that backlog, with the instruction to assign what is already
  written down rather than restate it as a new task.

### FR-P8: A worker asking the operator

A run cannot stop and wait for a person: `claude -p` is print mode, so there is
no channel back into a run once it is spawned, and every agent runs
`bypassPermissions`, so the CLI never pauses to ask either. What an agent can do
is finish and leave the question behind — which is what an issue already is.

Only the projectmanager has the plan protocol, so a worker uses a fenced block:

```` markdown
```ask
The one-line question
Any detail the operator needs to answer it.
```
````

- Parsed out of a **successful** run's result by `job_finished`, into an open
  `decision` issue with `created_by = "agent"`, inheriting the task's agent,
  branch and milestone so the board keeps its shape. A failed run reports its
  crash instead; one failure, one issue.
- Asking does not replace answering. The blocks stay in the stored result — the
  record should show what was asked.
- At most 5 questions per run. More than that is a malfunction, not a question,
  and the excess is dropped with a warning rather than kept silently.
- The convention is appended to every task's text rather than kept in a system
  prompt, because an agent may have been created without one.
- The console marks these `asked`, and answering one is resolving it.
- **The planning prompt shows recently answered issues**, not only open ones.
  Without that the answer would go nowhere: a resolved issue leaves the open list
  and would never be seen again.

### FR-P6: Importing documents

`POST /projects/{pid}/milestones/import` accepts
`{ paths?, dry_run?, tasks?, issues? }`. A path may be a file or a directory
(scanned for `*.md`, one level deep); nothing given means `docs/`.

Each document becomes **one milestone**, and the work it describes becomes rows
under that milestone:

- A list under a heading that names work — *task breakdown, acceptance criteria,
  checklist, to-do, deliverables* — becomes one **backlog task** each.
- A list under a heading that names an unknown — *open questions, risks,
  blockers, TBD* — becomes one **open issue** each, `decision` or `blocker`
  according to the heading.
- Prose is left alone. A document is mostly prose, and a parser that guessed at
  paragraphs would turn one design doc into fifty bad tasks.
- A ticked `- [x]` item is history, not work, and is skipped.
- The document's own `#` title names the document, not a section of it, so it
  never classifies — otherwise "Work Instruction: … and Tasks" would swallow the
  whole file, since headings inherit the kind of the one above.
- Each imported row carries the item's own text plus `(imported from
  <file> § <heading>)`; the agent that runs it has never seen the document.

Importing is idempotent per document: a milestone remembers the file it came
from, so a second run picks up only what is new, and its tasks and issues come
in exactly once with it. `dry_run` reports the counts without writing anything.

### FR-P7: The `cls/` directory

Every time an agent's state changes — created, deleted, given a task, starting
one, finishing one, or the server restarting — its snapshot is rewritten to
`{root_dir}/cls/agents/{name}.json`: configuration, what it is doing now, what
is queued behind it, its open issues, and what it has cost.

- Written whole and swapped in with `os.replace`, so a reader never sees half a
  file. The directory is created on demand by the first agent a project gains.
- The "finished" snapshot is written on a separate `worker_idle` hook rather
  than in `job_finished`: the worker still holds the job there, and a snapshot
  written then would record the agent as busy with something it had finished.
- It is a **mirror, not a source**. Nothing is ever read back, and a write that
  fails is logged and dropped — bookkeeping must never fail the request or the
  worker loop that triggered it.
- `agents.yaml` agents belong to no project directory and are not mirrored.

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
tasks(id PK, project_id FK, agent NULL, title, text, status, created_by,
      message_id NULL, result, error, cost_usd, created_at, started_at,
      finished_at,
      CHECK (status IN ('backlog','cancelled') OR agent IS NOT NULL))
```

`agent` and `message_id` are null while a task is in the backlog, and stay null
if such a task is cancelled — it was never anyone's. A CHECK cannot be altered
in place, so a database written before `backlog` existed has its `tasks` table
rebuilt at startup, detected by the constraint text itself.

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
- [ ] Importing a document creates one milestone, a backlog task per work item
      and an open issue per open question — and dispatches nothing.
- [ ] Importing the same document twice does not duplicate any of it.
- [ ] A backlog task can be assigned exactly once; the second attempt is `409`.
- [ ] An agent's `cls/agents/{name}.json` shows it idle after its task finishes,
      not still working on it.
- [ ] A `cls/` directory that cannot be written does not fail the request.
- [ ] Patching an agent's budget keeps its session id and its queue.
- [ ] A patch cannot grant an agent tools outside the project's `tool_policy`.
- [ ] Narrowing a `tool_policy` below what an agent already holds is refused.
- [ ] A worker's ```` ```ask ```` block becomes an open `decision` issue against
      its task, and prose containing a question mark does not.
- [ ] Resolving that issue puts the answer in the manager's next planning prompt.

## 7. Out of scope

- Automatic replanning (on task completion, file change, or timer).
- Inter-agent messaging; agents communicate only through the manager's plans.
- Per-project API keys or any multi-tenant separation.
- Rolling a plan back once applied.
