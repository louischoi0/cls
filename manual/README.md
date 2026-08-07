# Manual

Operator documentation for the Claude Code automation server.

| Document | Covers |
|----------|--------|
| [`api.md`](api.md) | Backend HTTP API — every endpoint, request and response shape, error code and dispatch rule |
| [`console.md`](console.md) | The browser console — how to launch and reach it, what each page does, how the frontend is built |

## Where everything else lives

| File | Purpose |
|------|---------|
| `README.md` | v1 specification — messages, agents, dispatch, logs |
| `docs/PROJECTS.md` | v2 specification — projects, project agents, tasks, planning |
| `docs/kds-backend.md` | Running the project store on KDS instead of SQLite |
| `OPERATING.md` | Running the thing: start/stop, the API key, where the spec and the installed CLI disagree |
| `agents.yaml` | The static agents, loaded once at startup |

Specs say what the system is supposed to do and why. This manual says how to use
what was built. Where they disagree, the code is right and something here needs
fixing.

## Sixty seconds

```bash
cd ~/cls && ./run.sh                        # API + console on 127.0.0.1:8787
                                            # browse to http://127.0.0.1:8787/
                                            # and paste the key when asked
KEY=$(cat ~/.cc-automation/api_key)
curl -H "X-API-Key: $KEY" localhost:8787/agents
```

## The shape of it

```
POST /messages          → tags route text to agents        → logs/{date}/{topic}.md
POST /projects          → a project, its root_dir and brief (overview.md)
POST /projects/{p}/plan → the projectmanager reads the brief, answers with a plan,
                          the server validates it and creates agents and tasks
GET  /tasks?status=     → what every project is doing, in one list
```

One agent runs one job at a time; different agents run in parallel. Projects,
agents and tasks are durable; plain message queues are not.
