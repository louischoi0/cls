# Work Instruction: Claude Code Automation Server

**Date:** 2026-08-07
**Status:** Draft v1
**Owner:** Louis

---

## 1. Purpose

Build an HTTP server running on the EC2 instance that allows external clients to send text into Claude Code sessions programmatically. Messages are routed to predefined agents (or specific sessions) based on tags. Each agent's work is persisted as Markdown logs organized by date and topic, and these logs are queryable through the same API.

## 2. Scope

- Single-operator system. No multi-tenant concerns.
- Runs on the existing EC2 instance where Claude Code is installed and authenticated.
- Agents are **predefined in configuration** — no dynamic agent creation via API in v1.
- Message delivery is **fire-and-forget**: the API accepts and queues the message, and results are consumed by reading the Markdown logs. (A per-message status endpoint is included for observability.)

## 3. Architecture Overview

```
External client
    │  HTTPS + X-API-Key
    ▼
API Server (FastAPI, single process)
    ├── Auth middleware        — API key check
    ├── Agent Registry         — loaded from agents.yaml
    ├── Dispatcher             — tag → target agent(s) → per-agent queue
    ├── Agent Runner (workers) — one worker per agent, serialized execution
    │       └── subprocess: claude -p --resume <session_id> --output-format json
    ├── Log Store              — logs/{YYYY-MM-DD}/{topic}.md
    └── Query API              — list/read logs
```

## 4. Functional Requirements

### FR-1: Tagged message input
- `POST /messages` accepts `{ "text": string, "tags": [string], "topic": string (optional) }`.
- Tag semantics, resolved in priority order:
  1. `session:<id>` — dispatch directly to that Claude Code session ID.
  2. `agent:<name>` or a bare tag matching an agent's name or tag list — dispatch to all matching agents.
  3. `global` — fan out to every registered agent.
- If no target resolves, respond `422` with an explanatory error. Do not silently drop.
- On success respond `202 Accepted` with a generated `message_id` and the resolved target list.

### FR-2: Predefined agent management
- Agents are defined in `agents.yaml`, loaded at server startup.
- Each agent definition includes: `name`, `tags`, `cwd`, `system_prompt` (optional), `allowed_tools`, `permission_mode`.
- Each agent maintains a persistent Claude Code session. Session IDs are stored in `state/sessions.json` and reused via `--resume` so conversation context survives server restarts.
- `GET /agents` returns the registry (name, tags, session status, queue depth).

### FR-3: Dispatch
- Each agent has its own FIFO queue (`asyncio.Queue`).
- One worker per agent processes messages **serially** — never resume the same session concurrently.
- Worker invocation shape:
  ```bash
  claude -p "<text>" \
    --resume <session_id> \
    --output-format json \
    --allowedTools "<from agent config>" \
    --permission-mode <from agent config> \
    --max-turns <safety cap, e.g. 25>
  ```
- If no session exists yet for the agent, run without `--resume`, then capture and persist the new session ID from the JSON result.
- On subprocess failure: record the error into the log entry, mark message status `failed`, continue with the next message. Retry is manual (re-send) in v1.

### FR-4: Markdown work logs
- **The Runner writes the logs, not the agent.** Do not rely on prompting Claude to save its own logs; the server appends deterministically from the JSON result of each run.
- Path scheme: `logs/{YYYY-MM-DD}/{topic}.md`
  - `topic` comes from the request field; falls back to the agent name if absent.
  - Sanitize `topic` to a safe slug (`[a-z0-9-]`) to prevent path traversal.
- Append one entry per processed message:
  ```markdown
  ## [HH:MM:SS] agent: <name> | message: <message_id>

  **Input:** <original text>

  **Result:**
  <claude result text>

  **Meta:** duration=<s>, cost_usd=<from JSON>, status=<ok|failed>

  ---
  ```
- Writes must be atomic per entry (single append call under a per-file lock).

### FR-5: External log query
- `GET /logs` → list of dates.
- `GET /logs/{date}` → list of topics for that date.
- `GET /logs/{date}/{topic}` → raw Markdown (`text/markdown`).
- `GET /messages/{message_id}` → status: `queued | running | done | failed`, target agents, timestamps.
- Validate `date` and `topic` path parameters strictly (reject `..`, absolute paths).

## 5. Authentication

- Single static API key.
- The key is stored **locally on the server** (environment variable or a file such as `~/.cc-automation/api_key`, permissions `600`). It is never hardcoded in the repository.
- Every request must carry the header `X-API-Key: <key>`.
- Middleware compares the presented key against the stored value using a **constant-time comparison** (`hmac.compare_digest`), and rejects mismatches with `401`.
- No key rotation mechanism in v1 — rotating means replacing the stored string and restarting.

## 6. Non-Functional Requirements

- **Network exposure:** bind to localhost or a private interface where possible; restrict the EC2 security group to the operator's IP. TLS via reverse proxy (Caddy/nginx) or an SSH tunnel/Tailscale if the API is not publicly exposed.
- **Concurrency:** per-agent serialization is mandatory; different agents may run in parallel.
- **Durability:** queued messages live in memory in v1. A server restart drops the queue (acceptable; documented). Logs and session state are on disk and survive restarts.
- **Observability:** structured server log (JSON lines) with message_id correlation.
- **Resource safety:** `--max-turns` cap on every invocation; subprocess timeout (e.g. 15 min) with kill + `failed` status.

## 7. Project Layout

```
cc-automation/
├── agents.yaml
├── server/
│   ├── main.py          # FastAPI app, routes, auth middleware
│   ├── registry.py      # agent config loading/validation
│   ├── dispatcher.py    # tag resolution, queues
│   ├── runner.py        # subprocess invocation, session persistence
│   ├── logstore.py      # md append + query
│   └── models.py        # pydantic schemas
├── state/
│   └── sessions.json
└── logs/
    └── 2026-08-07/
        └── <topic>.md
```

## 8. Task Breakdown

1. **Scaffold** — FastAPI app, config loading, auth middleware, health endpoint.
2. **Registry** — `agents.yaml` schema + validation; `GET /agents`.
3. **Dispatcher** — tag resolution rules with unit tests (session / agent / global / no-match).
4. **Runner** — subprocess wrapper for `claude -p`, JSON parsing, session ID persistence, timeout handling.
5. **Log Store** — append writer with slug sanitization and per-file lock; query endpoints.
6. **Message status** — in-memory status map keyed by `message_id`.
7. **Hardening** — constant-time key check, path validation, `--max-turns`, subprocess timeout.
8. **Smoke test** — end-to-end: POST a tagged message → verify dispatch → verify md entry → GET the log back.

## 9. Acceptance Criteria

- [ ] A request without a valid `X-API-Key` is rejected with `401`.
- [ ] `POST /messages` with `tags: ["global"]` reaches every registered agent.
- [ ] `POST /messages` with an agent tag reaches only matching agents; unknown tags return `422`.
- [ ] Two rapid messages to the same agent execute serially, in order.
- [ ] Each processed message produces exactly one entry in `logs/{date}/{topic}.md`.
- [ ] Agent conversation context persists across server restarts (verified via `--resume`).
- [ ] `GET /logs/{date}/{topic}` returns the raw Markdown created above.
- [ ] Path traversal attempts on log endpoints are rejected.

## 10. Out of Scope (v1)

- Dynamic agent creation/deletion via API.
- Durable message queue (Redis, SQLite).
- Webhook callbacks on completion.
- Multi-user auth, key rotation.
- Streaming responses to clients (long-lived `--input-format stream-json` processes) — candidate for v2 if resume latency becomes a problem.
