"""One FIFO queue per session.

What used to be here — `resolve_targets`, tag priority, `session:`/`agent:`
prefixes, `global` fan-out — existed so one message could reach several agents
at once. A chat turn goes to the session it was typed into, so the routing is
the URL and all that is left is the queues.

The rule that matters survives unchanged: **one worker per session, strictly
serial**. Two `claude --resume` processes on the same session id would corrupt
the conversation, so a session's turns are queued rather than run concurrently.
"""

from __future__ import annotations

import asyncio


class Dispatcher:
    def __init__(self) -> None:
        self._queues: dict[str, asyncio.Queue] = {}

    def add(self, name: str) -> asyncio.Queue:
        """The session's queue, created on first use and reused after that."""
        queue = self._queues.get(name)
        if queue is None:
            queue = asyncio.Queue()
            self._queues[name] = queue
        return queue

    def remove(self, name: str) -> None:
        self._queues.pop(name, None)

    def queue(self, name: str) -> asyncio.Queue:
        return self._queues[name]

    def depth(self, name: str) -> int:
        queue = self._queues.get(name)
        return queue.qsize() if queue is not None else 0

    async def send(self, name: str, job) -> None:
        await self.add(name).put(job)

    def __contains__(self, name: object) -> bool:
        return name in self._queues
