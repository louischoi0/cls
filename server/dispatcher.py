"""Tag resolution and per-agent queues.

`resolve_targets` is deliberately free of I/O and of any reference to the
running server, so the routing rules in README FR-1 can be tested directly.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Mapping

from .registry import Registry

SESSION_PREFIX = "session:"
AGENT_PREFIX = "agent:"
GLOBAL_TAG = "global"


class DispatchError(Exception):
    """No target resolved. Surfaced as 422 — never a silent drop."""

    def __init__(self, message: str, unmatched: list[str]) -> None:
        super().__init__(message)
        self.message = message
        self.unmatched = unmatched


@dataclass
class ResolvedTargets:
    agents: list[str]
    #: session id, when routing was driven by a `session:<id>` tag
    session_id: str | None = None
    #: tags that matched nothing, reported even when other tags did match
    unmatched: list[str] = field(default_factory=list)


def resolve_targets(
    tags: list[str],
    registry: Registry,
    sessions: Mapping[str, str],
) -> ResolvedTargets:
    """Resolve routing tags to agent names, in README FR-1 priority order.

    `sessions` maps agent name -> current session id.
    """
    # 1. session:<id> wins outright over every other tag.
    session_tags = [t for t in tags if t.startswith(SESSION_PREFIX)]
    if session_tags:
        by_session = {sid: name for name, sid in sessions.items()}
        agents: list[str] = []
        unmatched: list[str] = []
        matched_sid: str | None = None
        for tag in session_tags:
            sid = tag[len(SESSION_PREFIX) :]
            owner = by_session.get(sid)
            if owner is None:
                unmatched.append(tag)
            elif owner not in agents:
                agents.append(owner)
                matched_sid = sid
        if not agents:
            raise DispatchError(
                "no agent currently holds the requested session id", unmatched
            )
        return ResolvedTargets(agents=agents, session_id=matched_sid, unmatched=unmatched)

    # 2. agent:<name> / bare name / bare tag, and 3. global fan-out.
    agents = []
    unmatched = []
    for tag in tags:
        if tag == GLOBAL_TAG:
            matches = registry.names
        elif tag.startswith(AGENT_PREFIX):
            name = tag[len(AGENT_PREFIX) :]
            matches = [name] if name in registry.by_name else []
        else:
            matches = registry.match_tag(tag)

        if not matches:
            unmatched.append(tag)
            continue
        for name in matches:
            if name not in agents:
                agents.append(name)

    if not agents:
        raise DispatchError(
            f"no agent matched tags {tags!r}; "
            f"known agents: {registry.names}",
            unmatched,
        )
    return ResolvedTargets(agents=agents, unmatched=unmatched)


class Dispatcher:
    """Owns one FIFO queue per agent. One worker drains each queue."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self._queues: dict[str, asyncio.Queue] = {
            name: asyncio.Queue() for name in registry.by_name
        }

    def add(self, agent: str) -> asyncio.Queue:
        return self._queues.setdefault(agent, asyncio.Queue())

    def remove(self, agent: str) -> None:
        self._queues.pop(agent, None)

    def queue(self, agent: str) -> asyncio.Queue:
        return self._queues[agent]

    def depth(self, agent: str) -> int:
        return self._queues[agent].qsize()

    async def enqueue(self, agent: str, job) -> None:
        await self._queues[agent].put(job)
