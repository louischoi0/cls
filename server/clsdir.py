"""The `cls/` directory: what the server knows about a project's agents, on disk.

The database is the record, but it is not *in the project*. An agent working in
`root_dir` cannot query the store, an operator reading the repository cannot see
who is doing what, and neither can a script. So every time an agent's state
changes, its snapshot is rewritten to:

    <root_dir>/cls/agents/<name>.json

One file per agent, rewritten whole and swapped into place with `os.replace`, so
a reader never sees a half-written file. The directory is created on demand —
the first agent a project gains brings it into being.

This is a **mirror, not a source**. Nothing here is ever read back into the
server, and a write that fails is logged and dropped: bookkeeping must never
take down the worker loop that triggered it.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from .models import AgentState, utcnow

log = logging.getLogger("cc_automation.clsdir")

DIRNAME = "cls"
AGENTS_DIR = "agents"
README = """# cls/

Written by the cls server. **Do not edit** — every file here is regenerated
whenever the agent it describes changes state, and edits are overwritten.

- `agents/<name>.json` — one agent: its configuration, what it is doing right
  now, what is queued behind it, and what it has cost so far.

The server never reads this directory back; the database is the record.
"""
#: A snapshot is a status page, not an archive. Task results can run to
#: kilobytes, and the point of the file is that it can be read at a glance.
MAX_SNIPPET = 400
RECENT_TASKS = 10


def agents_dir(root_dir: str | Path) -> Path:
    return Path(root_dir) / DIRNAME / AGENTS_DIR


def snapshot(state: AgentState, short_name: str) -> dict:
    """One agent's state as the flat, stable shape written to disk.

    Purpose-built rather than a dump of `AgentState`: the file is a contract
    with whatever reads the repository, so it should not change shape every time
    an internal model gains a field.
    """
    running = state.running
    return {
        "agent": short_name,
        "runtime_name": state.name,
        "project": state.project,
        "role": state.role,
        "updated_at": utcnow().isoformat(),
        "config": {
            "cwd": state.cwd,
            "model": state.model,
            "allowed_tools": state.allowed_tools,
            "permission_mode": state.permission_mode,
            "max_budget_usd": state.max_budget_usd,
            "timeout_s": state.timeout_s,
        },
        "session_id": state.session_id,
        "activity": {
            "state": state.activity,
            "busy": state.busy,
            "detail": state.activity_detail,
            "since": _iso(state.activity_since),
            "task_id": state.working_on,
        },
        "queue": {
            "depth": state.queue_depth,
            "running": _task(running) if running else None,
            "queued": [_task(t) for t in state.queued],
        },
        "totals": {
            "tasks_done": state.tasks_done,
            "tasks_failed": state.tasks_failed,
            "cost_usd": state.cost_usd,
            "last_active": _iso(state.last_active),
        },
        "open_issues": [
            {"id": i.id, "title": i.title, "kind": i.kind, "created_at": _iso(i.created_at)}
            for i in state.open_issues
        ],
        "recent_tasks": [_task(t) for t in state.recent[:RECENT_TASKS]],
    }


def write(root_dir: str | Path, short_name: str, state: AgentState) -> Path:
    """Write one agent's snapshot, creating `cls/` if this is the first."""
    directory = agents_dir(root_dir)
    directory.mkdir(parents=True, exist_ok=True)
    readme = directory.parent / "README.md"
    if not readme.exists():
        readme.write_text(README, encoding="utf-8")

    path = directory / f"{short_name}.json"
    body = json.dumps(snapshot(state, short_name), indent=2, ensure_ascii=False)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(body + "\n", encoding="utf-8")
    os.replace(tmp, path)  # a reader sees the old file or the new one, never half
    return path


def remove(root_dir: str | Path, short_name: str) -> None:
    """Drop the snapshot of an agent that no longer exists."""
    path = agents_dir(root_dir) / f"{short_name}.json"
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        log.warning("could not remove %s: %s", path, exc)


def _task(task) -> dict:
    return {
        "id": task.id,
        "title": task.title,
        "status": task.status,
        "branch": task.branch,
        "milestone_id": task.milestone_id,
        "cost_usd": task.cost_usd,
        "created_at": _iso(task.created_at),
        "finished_at": _iso(task.finished_at),
        "outcome": (task.error or task.result or "")[:MAX_SNIPPET] or None,
    }


def _iso(value) -> str | None:
    return value.isoformat() if value else None
