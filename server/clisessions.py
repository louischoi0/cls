"""The Claude Code CLI's own sessions, on this machine.

Every session in this console is one CLI conversation and nothing else: the
server mints a session id with `--session-id` on a session's first turn, and
`claude` writes that conversation to

    ~/.claude/projects/<escaped cwd>/<session id>.jsonl

This module is the other half of that correspondence — it reads what is on disk,
so the link can be *shown and checked* rather than assumed. It is also what lets
a conversation you started in a terminal be adopted by the console instead of
being stranded there.

**The directory name is lossy.** `claude` escapes a cwd by replacing `/` and `.`
with `-`, so `/home/x/.config` and `/home/x-config` land in the same directory
and the mapping cannot be run backwards. Nothing here tries: the cwd is read out
of the records themselves, where it is stored verbatim, and the escape is only
ever computed forwards.

Nothing here writes. These files belong to the CLI, and deleting a session in
this console leaves its transcript on disk for `claude --resume` to find.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from .models import TurnStep

#: Where the CLI keeps conversations. Overridable so tests never read the real one.
DEFAULT_ROOT = Path("~/.claude/projects")

#: How much of a `.jsonl` is read from each end. A long conversation is
#: megabytes, and everything wanted here — the cwd, the title, the last prompt —
#: is near one edge or the other.
EDGE_BYTES = 128 * 1024


class CliSession(BaseModel):
    """One conversation on disk, as far as this server can see it."""

    session_id: str
    #: Read out of the records, not inferred from the directory name.
    cwd: str | None = None
    path: str
    #: The CLI's own generated title for the conversation, when it has made one.
    title: str | None = None
    #: The most recent thing said to it — the best one-line "what is this".
    last_prompt: str | None = None
    modified_at: datetime
    size_bytes: int
    #: The console session that owns this conversation. None means unadopted:
    #: it exists on disk and nothing here is driving it.
    owner: str | None = None


def root_dir(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser()
    env = os.environ.get("CC_AUTOMATION_CLI_SESSIONS_DIR")
    return Path(env).expanduser() if env else DEFAULT_ROOT.expanduser()


def project_dir_name(cwd: Path | str) -> str:
    """A cwd -> the directory `claude` files its conversations under.

    Forward only, and deliberately: `/` and `.` both become `-`, so two
    different cwds can produce one name and no inverse exists.
    """
    text = str(Path(cwd))
    return text.replace("/", "-").replace(".", "-")


def _edge_lines(path: Path) -> list[str]:
    """The first and last `EDGE_BYTES` of a file, as whole lines.

    A partial line at either seam is dropped rather than parsed: half a JSON
    record is not a record, and guessing at one would put invented text on a
    screen that is supposed to be showing what is on disk.
    """
    size = path.stat().st_size
    with path.open("rb") as handle:
        if size <= EDGE_BYTES * 2:
            chunks = [handle.read()]
        else:
            head = handle.read(EDGE_BYTES)
            handle.seek(-EDGE_BYTES, os.SEEK_END)
            tail = handle.read()
            # Drop the partial line each cut created.
            chunks = [head.rsplit(b"\n", 1)[0], tail.split(b"\n", 1)[-1]]
    lines: list[str] = []
    for chunk in chunks:
        lines.extend(chunk.decode("utf-8", "replace").splitlines())
    return lines


#: Parsed conversations, keyed by (path, mtime, size). The console polls the
#: session list every few seconds and every row wants a title; without this each
#: poll re-reads 256 KiB per session for an answer that has not changed.
_CACHE: dict[tuple[str, int, int], CliSession] = {}
_CACHE_MAX = 256


def read_session(path: Path, owner: str | None = None) -> CliSession | None:
    """One `.jsonl` -> what is worth knowing about it, or None if unreadable."""
    try:
        stat = path.stat()
    except OSError:
        return None

    key = (str(path), stat.st_mtime_ns, stat.st_size)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached.model_copy(update={"owner": owner})

    try:
        lines = _edge_lines(path)
    except OSError:
        return None

    cwd = title = last_prompt = None
    for line in lines:
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if not isinstance(record, dict):
            continue
        if cwd is None and isinstance(record.get("cwd"), str):
            cwd = record["cwd"]
        kind = record.get("type")
        # Both of these are rewritten as the conversation goes, so the last one
        # seen is the current one.
        if kind == "ai-title" and isinstance(record.get("aiTitle"), str):
            title = record["aiTitle"]
        elif kind == "last-prompt" and isinstance(record.get("lastPrompt"), str):
            last_prompt = record["lastPrompt"]

    session = CliSession(
        session_id=path.stem,
        cwd=cwd,
        path=str(path),
        title=title,
        last_prompt=last_prompt,
        modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
        size_bytes=stat.st_size,
        owner=owner,
    )
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()          # bounded, and a cold read costs one edge scan
    _CACHE[key] = session
    return session


def scan(root: Path | str | None = None, owners: dict[str, str] | None = None) -> list[CliSession]:
    """Every conversation on this machine, newest first.

    `owners` maps session id -> console session name, so each one comes back
    saying whether anything here is driving it.
    """
    base = root_dir(root)
    if not base.is_dir():
        return []
    owners = owners or {}
    found: list[CliSession] = []
    for project in sorted(base.iterdir()):
        if not project.is_dir():
            continue
        for path in project.glob("*.jsonl"):
            session = read_session(path, owner=owners.get(path.stem))
            if session is not None:
                found.append(session)
    found.sort(key=lambda s: s.modified_at, reverse=True)
    return found


def locate(session_id: str, root: Path | str | None = None) -> Path | None:
    """Where a conversation actually is, by id alone.

    **A session's transcript does not stay where its cwd says it will.** `claude`
    files a conversation under whatever directory it is *running in*, so an
    agent that steps into a git worktree — or anywhere else — moves its own
    transcript to a different project directory mid-conversation. Deriving the
    path from the session's configured cwd finds nothing in that case, and the
    console reports a live conversation as missing.

    So the id is the key and the directory is the search space. No file is
    opened here; this is a stat per project directory.
    """
    base = root_dir(root)
    if not base.is_dir():
        return None
    for project in sorted(base.iterdir()):
        if not project.is_dir():
            continue
        path = project / f"{session_id}.jsonl"
        if path.is_file():
            return path
    return None


def find(session_id: str, root: Path | str | None = None) -> CliSession | None:
    """One conversation by id, wherever on this machine it lives."""
    path = locate(session_id, root)
    return read_session(path) if path is not None else None


def path_for(session_id: str, cwd: Path | str, root: Path | str | None = None) -> Path:
    """Where a conversation *will* be written, before it exists.

    The forward escape is exact, so this is a real answer for a session that has
    an id but has not run yet — which is what the console shows as "no file yet".
    """
    return root_dir(root) / project_dir_name(cwd) / f"{session_id}.jsonl"


class CliTurn(BaseModel):
    """One turn of a CLI conversation, as the console replays it."""

    role: str
    text: str
    at: datetime | None = None
    steps: list[TurnStep] = []


def _blocks(content) -> list:
    return content if isinstance(content, list) else []


def _stamp(record: dict) -> datetime | None:
    raw = record.get("timestamp")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _tool_line(block: dict) -> str:
    """A `tool_use` block as one line, the way the CLI prints it."""
    name = block.get("name") or "tool"
    args = block.get("input")
    if isinstance(args, dict):
        for key in ("command", "file_path", "path", "pattern", "query", "url", "prompt"):
            value = args.get(key)
            if isinstance(value, str) and value.strip():
                return f"{name}({value.strip().splitlines()[0][:120]})"
    return f"{name}()"


def read_turns(path: Path | str, limit: int = 200) -> list[CliTurn]:
    """A conversation on disk -> its turns, oldest last, at most `limit`.

    What counts as a turn is the whole trick here. The CLI's `user` records are
    two different things wearing one name: a record whose `content` is a
    **string** is something a person typed, and one whose content is a list of
    `tool_result` blocks is the harness feeding a tool's output back to the
    model. Only the first is a turn; treating the second as one produces a
    transcript where the user appears to say `total 92 drwxrwxr-x ...`.

    Assistant text is the reply; `tool_use` and `thinking` become the steps
    under it, which is the same shape a live run streams.

    Streamed and kept in a ring, so a multi-megabyte conversation costs one pass
    and `limit` turns of memory rather than the whole file.
    """
    from collections import deque

    path = Path(path)
    turns: deque[CliTurn] = deque(maxlen=max(1, limit))
    said: list[str] = []
    steps: list[TurnStep] = []
    when: datetime | None = None

    def flush() -> None:
        nonlocal said, steps, when
        if said or steps:
            turns.append(CliTurn(
                role="agent", text="\n\n".join(said), at=when, steps=steps,
            ))
        said, steps, when = [], [], None

    try:
        handle = path.open("r", encoding="utf-8", errors="replace")
    except OSError:
        return []
    with handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            kind = record.get("type")
            message = record.get("message")
            if not isinstance(message, dict):
                continue

            if kind == "user":
                content = message.get("content")
                if isinstance(content, str):
                    # A person typed this: it closes whatever the model was
                    # saying and opens a turn of its own.
                    flush()
                    if content.strip():
                        turns.append(CliTurn(
                            role="user", text=content, at=_stamp(record),
                        ))
                else:
                    for block in _blocks(content):
                        if isinstance(block, dict) and block.get("type") == "tool_result":
                            steps.append(TurnStep(
                                kind="tool_result",
                                text="error" if block.get("is_error") else "ok",
                            ))
            elif kind == "assistant":
                when = when or _stamp(record)
                for block in _blocks(message.get("content")):
                    if not isinstance(block, dict):
                        continue
                    btype = block.get("type")
                    if btype == "text" and str(block.get("text") or "").strip():
                        said.append(str(block["text"]).strip())
                    elif btype == "tool_use":
                        steps.append(TurnStep(kind="tool", text=_tool_line(block)))
                    elif btype == "thinking":
                        steps.append(TurnStep(kind="thinking", text="thinking"))
    flush()
    return list(turns)
