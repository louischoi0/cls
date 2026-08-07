# Console manual (frontend)

The browser UI for the automation server. It drives the same HTTP API described
in [`api.md`](api.md) — there is nothing it can do that a `curl` cannot.

---

## Contents

1. [Launching it](#1-launching-it)
2. [Reaching it from your laptop](#2-reaching-it-from-your-laptop)
3. [The API key](#3-the-api-key)
4. [The pages](#4-the-pages)
5. [Refresh and polling](#5-refresh-and-polling)
6. [How it is built](#6-how-it-is-built)
7. [Configuration](#7-configuration)
8. [Development](#8-development)
9. [Troubleshooting](#9-troubleshooting)

---

## 1. Launching it

**There is nothing separate to launch.** The console is four static files in
`web/` served by the same FastAPI process as the API. Start the server and the
UI is up.

```bash
cd ~/cls
./run.sh
```

Then open **http://127.0.0.1:8787/** — `/` redirects to `/web/`.

```
started with agents: researcher, ops        # the server is ready
```

Other ports and interfaces:

```bash
CC_AUTOMATION_PORT=9000 ./run.sh            # http://127.0.0.1:9000/
CC_AUTOMATION_HOST=0.0.0.0 ./run.sh         # all interfaces — read §2 first
./run.sh --reload                           # extra args go through to uvicorn
```

Stopping it:

```bash
pkill -f 'uvico[r]n server.main'
```

The bracket keeps `pkill -f` from matching the shell that runs the command and
killing your own session.

**No build step, no node, no npm.** Editing `web/app.js` and reloading the page
is the whole edit cycle; the files are read from disk per request. There is no
bundler to run and nothing to install on the EC2 box.

---

## 2. Reaching it from your laptop

`run.sh` binds `127.0.0.1` by default (README §6). The console is a browser app,
so you need that port on the machine running the browser. In order of
preference:

**SSH tunnel** — nothing is exposed, no configuration on the server:

```bash
ssh -N -L 8787:127.0.0.1:8787 ec2-user@<host>
# then open http://127.0.0.1:8787/ on your laptop
```

**Tailscale** — bind to the tailnet address and reach it by machine name.

**Reverse proxy with TLS** (Caddy/nginx) plus a security group limited to your
IP, if the console needs to be reachable without a tunnel.

Do not put `CC_AUTOMATION_HOST=0.0.0.0` on a public interface without one of the
above. The API key is the only authentication there is, it is static, and it
would be travelling in clear text over plain HTTP.

---

## 3. The API key

The console asks for the key the first time it loads and stores it in that
browser's `localStorage` under `cls.apiKey`. It is sent as `X-API-Key` on every
request the page makes. The ⚿ button in the top bar reopens the dialog to change
it; any `401` reopens it automatically.

```bash
cat ~/.cc-automation/api_key      # the value to paste
```

**`/web/*` is served without authentication.** A browser cannot attach a header
to a navigation, so the shell has to be reachable before a key exists. Those
files are HTML, CSS and JavaScript with no secrets in them, and the API
underneath stays closed — an unauthenticated visitor gets a page that can do
nothing. What protects the box is §2, not the console.

Clearing the key: `localStorage.removeItem('cls.apiKey')` in the browser console,
or paste an empty value.

---

## 4. The pages

Navigation is hash-based (`#/projects/demo`), so links and the back button work
and a reload keeps your place.

### Projects — `#/`

Cards for every project: manager, agent count, open tasks. **New project** at the
bottom takes a name, a root directory that must already exist on the server, the
tool policy, and the projectmanager to create alongside it.

The **tool policy** field is the important one. It caps what any agent in the
project can be granted and cannot be widened afterwards — changing it means
recreating the project.

### Project — `#/projects/{id}`

Five sections, top to bottom:

**Planning round.** *Run planning round* invokes the projectmanager with
`overview.md` and the current state. The button blocks while it runs (the manager
is a real `claude` invocation — up to its `timeout_s`, 900s by default) and then
shows the summary, the applied actions, the rejected ones with reasons, and the
raw reply in a collapsed block.

Read the rejected list. A plan half-applies by design: a bad action is rejected
on its own rather than sinking the round, so a successful-looking result can
still contain refusals.

**Agents.** The roster with roles, working directories and tools. *Add an agent*
takes a name, role, a `cwd` relative to the project root, tools, a system prompt,
a budget and a timeout. Leaving tools blank inherits the project policy — it does
not mean no tools. The manager option is disabled once one exists. *Remove* is
refused while that agent has a running task.

**Tasks.** Click any row for the full instruction, result, error, timings and
cost. Queued tasks have a *Cancel* button; running ones do not, because
cancellation only avoids work that has not started. *Assign a task* is hidden
until the project has a worker.

**overview.md.** A plain editor over the file at `{root_dir}/overview.md`. This
is the brief the manager plans from, so editing it here is the main way to steer
a project. *Save* writes the file; it does not trigger a plan.

### Agents — `#/agents`

Every agent the dispatcher can reach — `agents.yaml` agents and project agents
together — with queue depth, busy state and session id. Below it, **Send a
message**: the project-free path from README FR-1. Tags route it — an agent name,
a tag from `agents.yaml`, `project:<id>`, or `global`. Results go to the logs, not
back to this page.

### Tasks — `#/tasks`

Every project's work in one table, filterable by status and project. The filters
live in the URL (`#/tasks?status=failed`), so a filtered view is linkable.

### Logs — `#/logs`

Date, then topic, then the raw Markdown the runner wrote. Task runs appear under
the project id; planning rounds under `{project_id}-plan`.

---

## 5. Refresh and polling

Task and agent tables refresh every 3 seconds. Only the table body is replaced,
so an open form keeps its content and the cursor stays where you left it.

Everything else refreshes when you act or navigate. The dot in the top bar is
grey when idle, blinks blue while requests are in flight, and turns red when the
server cannot be reached.

---

## 6. How it is built

```
web/
├── index.html    the shell: top bar, nav, key dialog, toast host
├── render.js     pure: escaping, formatting, every HTML fragment
├── app.js        API client, router, views, DOM and events
└── style.css     light/dark, one file, no external fonts
```

Vanilla JavaScript, no framework, no dependencies. The reason is the deployment:
one EC2 box with no node toolchain, so a build step would be a thing to install,
run and keep working for no benefit at this size.

**The `render.js` / `app.js` split is deliberate.** There is no node here to run
a test suite against, so the pure half lives in its own file and is tested in
QuickJS from Python (`tests/test_web.py`) with its output asserted directly.
Everything that touches `document` lives in `app.js`, which gets a parse check
and a test that every element id it reaches for actually exists. If you add a
pure helper, put it in `render.js` where it can be tested.

**Escaping.** All markup is built with an `html` tagged template that escapes
every interpolation. Task titles and results are written by Claude, so they are
untrusted; they reach the DOM as text, never as markup. To pass markup through
deliberately, wrap it in `raw()`. Nested `html` results are passed through
automatically — the function returns a String object carrying a `__raw` marker.

**Routing** is a table of regexes over `location.hash` in `app.js`. **State** is
not cached: each view fetches what it needs on entry.

---

## 7. Configuration

| Variable | Default | Effect |
|----------|---------|--------|
| `CC_AUTOMATION_HOST` | `127.0.0.1` | Interface `run.sh` binds |
| `CC_AUTOMATION_PORT` | `8787` | Port |
| `CC_AUTOMATION_WEB_DIR` | `<repo>/web` | Where the console's files are read from |

Pointing `CC_AUTOMATION_WEB_DIR` at a directory that does not exist runs the
server headless: `/web/` returns `404`, the API is untouched, and a warning is
logged at startup.

Browser-side state, both in `localStorage`:

| Key | Value |
|-----|-------|
| `cls.apiKey` | The API key |
| `cls.theme` | `light` or `dark`; absent means follow the OS |

---

## 8. Development

```bash
./run.sh --reload                     # uvicorn restarts on Python changes
.venv/bin/python -m pytest tests/test_web.py -q
```

Static files are read per request, so a browser reload picks up edits to
`web/*` — `--reload` is only for the Python side. Use a hard reload
(<kbd>Ctrl</kbd>/<kbd>Cmd</kbd>+<kbd>Shift</kbd>+<kbd>R</kbd>) if a stale
`app.js` is cached.

`tests/test_web.py` covers both halves: that the server serves the console
without a key while keeping the API closed and the source tree unreachable, and
that `render.js` escapes hostile input, builds the right table markup, and reads
every API error shape.

Before committing a change to `web/`, run the suite. A JavaScript syntax error
would otherwise show up only as a blank page.

---

## 9. Troubleshooting

**Blank page, nothing in the UI.** A JS syntax error. Open the browser console;
`pytest tests/test_web.py` catches this class of failure.

**Everything is `401`, the key dialog keeps reopening.** The pasted key does not
match the server's. Compare with `cat ~/.cc-automation/api_key`; if you changed
the file, the server needs a restart to pick it up.

**`404` at `/web/`.** The server booted without a console. Check the startup log
for `no console: ... is not a directory` and your `CC_AUTOMATION_WEB_DIR`.

**The red dot / "cannot reach the server".** The process died or the tunnel
dropped. Check it is listening: `curl -s localhost:8787/health`.

**A planning round hangs.** Expected — the manager is a real `claude` run and can
take minutes. It ends by its `timeout_s` (900s default) at the latest, and the
server answers `504` if the run outlasts `timeout_s + 60`.

**A task sits at `queued` and never runs.** Its worker is busy with an earlier
task; one agent runs one job at a time by design. Check `queue_depth` and `busy`
on the Agents page. If nothing is busy either, the server was started with
workers disabled.

**"Remove" or "Delete project" refused.** Something of theirs is `running`.
Cancelling a worker mid-run would abandon its `claude` subprocess rather than
kill it, so the wait is deliberate; `timeout_s` is the backstop.
