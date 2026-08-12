# cls — a chat console for Claude Code sessions

A small HTTP server and a browser console for talking to several Claude Code
sessions on one machine. You create a session, chat with it, and delete it when
it has served its purpose. The server keeps each conversation resumable across
restarts and keeps a transcript you can read back.

It used to be a project-management service — projects, agents, tasks, issues,
milestones, planning rounds. That is gone. What is left is the part that was
actually used: the chat.

## What a session is

One session is one Claude Code conversation, plus the settings it runs under:

| Field | Means |
|-------|-------|
| `name` | Identity, URL segment and log topic. `[A-Za-z0-9_-]`, up to 64 chars. |
| `cwd` | Where the `claude` subprocess runs. Must exist when the session is created. |
| `allowed_tools` | The real containment boundary — exactly what it may use. |
| `permission_mode` | Never one that can prompt: `claude -p` has nobody to ask, so a prompt would hang the turn until `timeout_s`. |
| `system_prompt` | Appended to Claude Code's own prompt, never substituted for it. |
| `model` | `claude --model`. Unset leaves the CLI on its default. |
| `max_budget_usd` | Per-turn spend cap. The installed CLI has no `--max-turns`. |
| `timeout_s` | Hard wall-clock kill for one turn. |

The Claude Code session id is minted on the first turn and reused with
`--resume` after that, so a session remembers what you said last week even
though the server has restarted since.

**Turns are serialised per session.** Two `claude --resume` processes on one
session id would corrupt the conversation, so a session has one FIFO queue and
one worker. Different sessions run in parallel.

## The API

Every request needs `X-API-Key`, except `GET /health` and the console's own
files. `server/sealed.py` describes the alternative: sealed envelopes that keep
the key off the wire entirely.

```
GET    /health                          no key needed
GET    /sessions                        list, with queue depth and turn counts
POST   /sessions                        create one
GET    /sessions/{name}
PATCH  /sessions/{name}                 change settings; absent fields unchanged
DELETE /sessions/{name}                 the session and its transcript
GET    /sessions/{name}/history         turns, oldest first (?limit=, tail)
DELETE /sessions/{name}/history         forget the transcript, keep the session
POST   /sessions/{name}/messages        say something -> 202 + message_id
GET    /messages/{id}                   queued | running | done | failed
GET    /messages/{id}/stream            SSE: the reply as it is written
GET    /logs, /logs/{date}, /logs/{date}/{topic}
```

```bash
KEY=$(cat ~/.cc-automation/api_key)

curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' -d '{
  "name": "research",
  "cwd": "/home/cdkbs/workspaces/researcher",
  "allowed_tools": ["Read", "Glob", "Grep"]
}' localhost:8787/sessions

curl -H "X-API-Key: $KEY" -H 'Content-Type: application/json' \
  -d '{"text":"what changed in this repo today?"}' \
  localhost:8787/sessions/research/messages

curl -H "X-API-Key: $KEY" localhost:8787/sessions/research/history
```

Deleting a session while a turn is running comes back `409`. Cancelling mid-run
would abandon the `claude` subprocess rather than kill it, so the wait is
deliberate; `timeout_s` guarantees it ends.

## The console

`http://127.0.0.1:8787/` — sessions down the left, one conversation on the
right, Enter to send. Three static files in `web/`, served by the same process:
no node, no build step, nothing to install.

`/web/*` is served **unauthenticated** — a browser cannot put a header on a
navigation, so the shell has to load before the key exists. It holds no secrets,
and the API under it still demands the key.

## Where things are kept

| What | Where | Survives a restart |
|------|-------|--------------------|
| Sessions and transcripts | `state/chat.db` (SQLite, WAL) | yes |
| Claude Code session ids | `state/sessions.json` | yes |
| Markdown work logs | `logs/{YYYY-MM-DD}/{session}.md` | yes |
| Queued turns | memory | **no** |
| Message status | memory, bounded | **no** |
| Live output | memory, bounded ring | **no** |

## Running it

`OPERATING.md` has the detail — the API key, the sealed transport, TLS, and the
things worth knowing before relying on any of it.

```bash
./run.sh                      # 127.0.0.1:8787
CC_AUTOMATION_PORT=9999 ./run.sh
.venv/bin/python -m pytest -q
```
