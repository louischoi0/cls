# Operating the automation server

Implementation notes for the spec in `README.md`. Only things an operator needs
that the spec does not already say.

## Start / stop

```bash
./run.sh                      # 127.0.0.1:8787
CC_AUTOMATION_PORT=9000 ./run.sh
```

Stop it with `pkill -f 'uvico[r]n server.main'` — the bracket keeps `pkill -f`
from matching the shell that runs it and killing your own session.

## The API key

Read from `CC_AUTOMATION_API_KEY`, else from `~/.cc-automation/api_key`
(mode `600`, generated during setup). Rotating it means replacing that string
and restarting. Every request except `GET /health` needs `X-API-Key`.

```bash
KEY=$(cat ~/.cc-automation/api_key)
curl -H "X-API-Key: $KEY" localhost:8787/agents
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text":"summarise today","tags":["research"],"topic":"daily"}' \
  localhost:8787/messages
curl -H "X-API-Key: $KEY" localhost:8787/logs/$(date +%F)/daily
```

## The project store

Spec: `docs/kds-backend.md`. Projects, agents and tasks live in **KDS**
(github.com/louischoi0/ckdbs); SQLite is the fallback and what the tests run on.
The backend is a URL:

```bash
CC_AUTOMATION_STORE='kds://127.0.0.1:15432?fallback=sqlite'   # the default
CC_AUTOMATION_STORE='sqlite://state/projects.db'              # no engine needed
```

Start the engine before the server:

```bash
/path/to/ckdbs/build/kds_server ~/.cc-automation/kds.db --port 15432 &
./run.sh
curl -s localhost:8787/health      # {"status":"ok","store":"kds"}
```

- **`/health` names the live backend.** That is the only way to tell that a
  fallback fired, so check it rather than assuming.
- **`fallback=sqlite` is opt-in and loud.** Without it, an unreachable engine is
  a startup failure naming the address. With it, the server logs at `ERROR` and
  comes up on SQLite — and the two databases *do not sync*, so anything written
  while the fallback is live is not in KDS.
- **Stop the engine by PID, not `pkill -f kds_server`** — that pattern matches
  any shell whose command line mentions the binary, including the one you are
  typing in. The same trap as `uvico[r]n` above.
- **One guarantee is weaker than on SQLite.** KDS has no `UNIQUE` and, by
  design, no `CREATE INDEX`, so "one projectmanager per project" is a check
  inside a transaction rather than a schema constraint. It holds for this server
  — one process, one connection, one lock — and would not hold against a second
  writer on the same database. `docs/kds-backend.md` §4.
- **`SYNC` is what makes a non-`INSERT` durable.** `UPDATE` and `DELETE` are not
  logged per statement, so a hard kill can lose status transitions that inserts
  of the same age would keep.

Tests: `CC_AUTOMATION_KDS_BIN=/path/to/kds_server` makes
`tests/test_store_contract.py` run its whole suite against both backends. Without
it, the KDS half skips.

## The console

A browser UI for all of the above, at **http://127.0.0.1:8787/** (`/` redirects
to `/web/`). It ships as three static files in `web/` that the same FastAPI
process serves — no node, no build step, nothing to install on the box.

Paste the API key once when it asks; it is kept in that browser's localStorage
and sent as `X-API-Key` on every call. Note that `/web/*` itself is served
**unauthenticated** — a browser cannot put a header on a navigation, so the
shell has to be reachable before the key exists. It contains no secrets, and the
API underneath is still closed. If the box is exposed beyond localhost, the
security group and the reverse proxy are what protect it (README §6), not the
console.

What it covers: projects and their agents, assigning and cancelling tasks,
running a planning round and reading the applied/rejected breakdown, the
cross-project task list, `overview.md` editing, the agent registry with a
send-message form, and the log browser. Task tables refresh every 3 seconds.

`CC_AUTOMATION_WEB_DIR` overrides the directory; point it somewhere that does
not exist to run headless, and `/web/` simply 404s.

## Projects

Spec: `docs/PROJECTS.md`. A project owns a `root_dir`, an `overview.md`, its
agents and its tasks. State lives in the project store (KDS by default — see
**The project store** above).

```bash
# create a project and its one projectmanager
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{
  "name": "Demo Proj",
  "root_dir": "/home/ec2-user/workspaces/demo",
  "tool_policy": ["Read","Glob","Grep","Edit","Write"],
  "manager": {"name":"pm","role":"manager","allowed_tools":["Read","Glob","Grep"]}
}' localhost:8787/projects

curl -H "X-API-Key: $KEY" -X PUT --data-binary @overview.md \
  localhost:8787/projects/demo-proj/overview

# one planning round: the manager reads overview.md and answers with a plan,
# the server validates and applies it
curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"note":"focus on the API"}' localhost:8787/projects/demo-proj/plan

curl -H "X-API-Key: $KEY" 'localhost:8787/tasks?status=failed'   # across projects
```

Operator-facing points:

- **`tool_policy` is the ceiling.** The manager can hand its workers tools, but
  only from this list, and it is set when the project is created. Widening it
  means recreating the project. Policy is matched on the base name, so `Bash`
  in the policy permits `Bash(ls *)` on an agent.
- **Nothing replans by itself.** `POST /projects/{id}/plan` is the only trigger;
  chain it from task completion yourself if you want a loop.
- Runtime agent names are `{project_id}__{agent_name}` — that is what appears in
  `GET /agents`, in `state/sessions.json`, and in the log entries. Project
  agents are also routable from `POST /messages` via `project:{id}`.
- Task runs log under topic `{project_id}`; planning rounds under
  `{project_id}-plan`.
- Deleting an agent or a project is refused while one of its tasks is running.
  Cancelling a worker mid-run would abandon its `claude` subprocess rather than
  kill it, so the wait is deliberate; `timeout_s` is the backstop.
- A restart re-enqueues `queued` tasks and marks `running` ones `failed` — their
  subprocess died with the server.

## Where the spec and the installed CLI disagree

- **`--max-turns` does not exist** in Claude Code 2.1.223, so FR-3's turn cap is
  implemented as `max_budget_usd` (a real `--max-budget-usd` flag) plus a
  wall-clock `timeout_s` that kills the process group.
- **`system_prompt` is appended, not substituted.** The CLI's `--system-prompt`
  replaces Claude Code's own system prompt wholesale, which strips the agent of
  its tool instructions. `agents.yaml`'s `system_prompt` maps to
  `--append-system-prompt`.
- **Session ids are minted by the server** using `--session-id` on an agent's
  first run, rather than captured afterwards as FR-3 describes. The id is
  written to `state/sessions.json` before the process starts, so a kill mid-run
  still leaves a resumable session. If a stored session ever goes missing, the
  worker mints a fresh one, retries once, and notes the reset in the log entry.

## Behaviour worth knowing

- `permission_mode` must never be one that can prompt. `claude -p` has nobody to
  ask, so a prompt hangs the worker until `timeout_s`. The shipped agents use
  `bypassPermissions`; their `allowed_tools` list is the actual boundary.
- A fan-out message with no `topic` is logged under `global`, not split per
  agent, so one message stays in one file.
- Queued messages are in memory. A restart drops the queue (README §6); logs and
  sessions are on disk and survive.
- Path traversal on the log routes comes back `404` rather than `400` when the
  URL never matches the route at all. Nothing outside `logs/` is readable either
  way.

## Tests

```bash
.venv/bin/python -m pytest -q
```

435 tests, 47 of which skip without a `kds_server` binary (see **The project
store**). They use a fake `claude` script (and, for the project tests, an
`AgentWorker` whose subprocess is a scripted reply), so they spend nothing and
need no network. That also means the planning round has never been exercised
against the real CLI — the JSON-plan parser is tested against the shapes models
actually emit, not against a live manager.

The console's `render.js` is tested too: there is no node here, so the JS runs
in QuickJS (the `quickjs` package) and its output is asserted directly. That is
why every pure function lives in `render.js` and everything touching the
document lives in `app.js` — only the first half can be tested this way, and
the browser behaviour of `app.js` is unverified beyond a parse check.
