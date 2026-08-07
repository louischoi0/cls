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

75 tests. They use a fake `claude` script, so they spend nothing and need no
network.
