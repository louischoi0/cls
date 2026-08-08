"""Live output from a run, on its way to whoever is watching.

`claude --output-format stream-json` emits one JSON object per line as the run
happens. This module turns those into a small normalised event and fans them out
to any number of readers, so the console can show what an agent is doing instead
of a spinner that ends in a wall of text.

Three rules shape it:

**The worker never waits for a reader.** Publishing is synchronous and cannot
block: a subscriber whose queue is full loses its oldest events and is told how
many. A browser on a slow connection must not hold up a `claude` subprocess.

**A reader that arrives late still sees the run.** Each run keeps a bounded ring
of what it has emitted, and a new subscriber gets that history before the live
feed. Opening a task the moment it starts and opening it ten seconds later
differ only in how much scrollback there is.

**Nothing here is durable.** The Markdown log and the task row remain the record
(README FR-4). This is a window onto a run in flight, held in memory, bounded in
both directions, and dropped when the process ends.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator

from .models import utcnow

log = logging.getLogger("cc_automation.stream")

#: Events kept per run for readers that arrive late.
BUFFER = 400
#: Runs kept after they finish, newest first.
RETAIN = 32
#: How far behind one reader may fall before it starts losing events.
SUBSCRIBER_QUEUE = 200
#: One event is a line on a page, not a document.
MAX_TEXT = 2000

#: What a reader is shown, distinct from the CLI's own vocabulary so the console
#: never has to know the shape of `claude`'s JSON.
KINDS = ("start", "thinking", "text", "tool", "tool_result", "notice", "result", "end")


@dataclass
class StreamEvent:
    seq: int
    kind: str
    text: str
    at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "text": self.text,
            "at": self.at.isoformat(),
        }


class Run:
    """One invocation's events, plus whoever is currently watching."""

    def __init__(self, key: str, agent: str, buffer: int = BUFFER) -> None:
        self.key = key
        self.agent = agent
        self.buffer = buffer
        self.events: list[StreamEvent] = []
        self.subscribers: set[asyncio.Queue] = set()
        self.done = False
        self.seq = 0
        #: events the ring dropped, so a reader can be told its history is partial
        self.dropped = 0
        self.started_at = utcnow()

    def publish(self, kind: str, text: str) -> StreamEvent:
        self.seq += 1
        event = StreamEvent(seq=self.seq, kind=kind, text=text[:MAX_TEXT])
        self.events.append(event)
        if len(self.events) > self.buffer:
            del self.events[0]
            self.dropped += 1

        for queue in list(self.subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # The reader is behind. Drop its oldest rather than block the
                # worker or disconnect it — a gap beats a stall.
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass
        return event


class StreamHub:
    """Every run that is live or recent enough to still be worth reading.

    Bounded like `StatusStore`: a long uptime must not grow it forever, and the
    oldest finished run is the one nobody is reading.
    """

    def __init__(self, retain: int = RETAIN, buffer: int = BUFFER) -> None:
        self.retain = retain
        self.buffer = buffer
        self._runs: OrderedDict[str, Run] = OrderedDict()

    def open(self, key: str, agent: str) -> Run:
        """Begin a run, replacing anything held under the same key."""
        run = Run(key, agent, buffer=self.buffer)
        self._runs[key] = run
        self._runs.move_to_end(key)
        self._evict()
        run.publish("start", f"{agent} started")
        return run

    def _evict(self) -> None:
        while len(self._runs) > self.retain:
            _, oldest = self._runs.popitem(last=False)
            # Wake its readers so they finish rather than hang on a run that no
            # longer exists.
            oldest.done = True
            for queue in list(oldest.subscribers):
                try:
                    queue.put_nowait(None)
                except asyncio.QueueFull:
                    pass

    def get(self, key: str) -> Run | None:
        return self._runs.get(key)

    def publish(self, key: str, kind: str, text: str) -> None:
        run = self._runs.get(key)
        if run is not None and not run.done:
            run.publish(kind, text)

    def close(self, key: str, text: str = "") -> None:
        run = self._runs.get(key)
        if run is None or run.done:
            return
        run.publish("end", text or "finished")
        run.done = True
        for queue in list(run.subscribers):
            try:
                queue.put_nowait(None)  # the sentinel that ends a subscription
            except asyncio.QueueFull:
                pass

    async def subscribe(self, key: str) -> AsyncIterator[StreamEvent]:
        """History, then whatever comes next, until the run ends.

        A run that has already finished yields its history and stops, which is
        what makes opening a task after the fact behave the same as watching it.
        """
        run = self._runs.get(key)
        if run is None:
            return
        if run.done:
            for event in list(run.events):
                yield event
            return

        # Subscribing before the history snapshot is what closes the gap: an
        # event published in between lands in the queue *and* in the snapshot,
        # and `seq` sorts out the duplicate below. There is no await between the
        # `done` check and this line, so a `close()` cannot slip past it —
        # publish and close are synchronous and run on this same loop.
        queue: asyncio.Queue = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE)
        run.subscribers.add(queue)
        try:
            last = 0
            for event in list(run.events):
                last = event.seq
                yield event
            while True:
                event = await queue.get()
                if event is None:  # the sentinel `close` pushes
                    return
                if event.seq <= last:
                    continue  # already sent as history
                last = event.seq
                yield event
        finally:
            run.subscribers.discard(queue)


def describe(payload: dict) -> tuple[str, str] | None:
    """One `claude` stream-json object -> `(kind, text)`, or None to ignore it.

    The CLI's vocabulary is wider than a reader needs and wider than it promises
    to keep. Everything unrecognised is dropped rather than shown raw, so a new
    event type is silence rather than noise.
    """
    kind = payload.get("type")

    if kind == "system":
        if payload.get("subtype") == "init":
            model = payload.get("model") or "?"
            return "notice", f"session started on {model}"
        return None

    if kind == "assistant":
        return _from_content(payload.get("message"))

    if kind == "user":
        # Tool results come back as a user turn; the interesting part is whether
        # the tool worked, not the payload, which can be a whole file.
        return _from_tool_result(payload.get("message"))

    if kind == "result":
        if payload.get("is_error"):
            errors = payload.get("errors")
            detail = "; ".join(str(e) for e in errors) if isinstance(errors, list) else ""
            return "result", detail or str(payload.get("subtype") or "failed")
        cost = payload.get("total_cost_usd")
        spent = f" · ${cost:.4f}" if isinstance(cost, (int, float)) else ""
        return "result", f"done{spent}"

    return None


def _from_content(message) -> tuple[str, str] | None:
    if not isinstance(message, dict):
        return None
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text" and str(block.get("text") or "").strip():
            return "text", str(block["text"]).strip()
        if btype == "thinking":
            return "thinking", "thinking"
        if btype == "tool_use":
            return "tool", f"{block.get('name') or 'tool'} {_tool_input(block.get('input'))}"
    return None


def _from_tool_result(message) -> tuple[str, str] | None:
    if not isinstance(message, dict):
        return None
    blocks = message.get("content")
    if not isinstance(blocks, list):
        return None
    for block in blocks:
        if isinstance(block, dict) and block.get("type") == "tool_result":
            return ("tool_result", "error" if block.get("is_error") else "ok")
    return None


def _tool_input(value) -> str:
    """The one field of a tool call worth putting on a line."""
    if not isinstance(value, dict):
        return ""
    for key in ("command", "file_path", "path", "pattern", "query", "url", "prompt"):
        found = value.get(key)
        if isinstance(found, str) and found.strip():
            return found.strip().splitlines()[0][:160]
    try:
        return json.dumps(value)[:160]
    except (TypeError, ValueError):
        return ""
