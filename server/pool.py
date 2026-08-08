"""The runtime set of agents: registry entry, queue and worker as one unit.

`agents.yaml` agents and project agents differ only in where their definition
came from. Below this line nothing knows the difference — a project agent gets
a FIFO queue, one serial worker and its own resumable session, exactly as
README FR-3 requires of any agent.

Adding or removing an agent touches three structures that must not drift apart,
so it happens in exactly one place.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable

from .dispatcher import Dispatcher
from .logstore import LogStore
from .models import AgentConfig
from .registry import Registry
from .runner import AgentWorker, JobObserver, SessionStore
from .stream import StreamHub

log = logging.getLogger("cc_automation.pool")


class AgentPool:
    def __init__(
        self,
        *,
        registry: Registry,
        dispatcher: Dispatcher,
        sessions: SessionStore,
        logstore: LogStore,
        status,
        claude_bin: str,
        worker_factory: Callable[..., AgentWorker] = AgentWorker,
        start_workers: bool = True,
        observer: JobObserver | None = None,
        env_provider=None,
        hub: StreamHub | None = None,
    ) -> None:
        self.registry = registry
        self.dispatcher = dispatcher
        self.sessions = sessions
        self.logstore = logstore
        self.status = status
        self.claude_bin = claude_bin
        self.worker_factory = worker_factory
        self.start_workers = start_workers
        self.observer = observer
        self.env_provider = env_provider
        #: shared by every worker; a run is keyed by message id, which is unique
        self.hub = hub or StreamHub()
        self.workers: dict[str, AgentWorker] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def start(self, agent: AgentConfig) -> AgentWorker:
        """Give an already-registered agent its queue and worker."""
        worker = self.worker_factory(
            agent=agent,
            queue=self.dispatcher.add(agent.name),
            sessions=self.sessions,
            logstore=self.logstore,
            status=self.status,
            claude_bin=self.claude_bin,
            observer=self.observer,
            env_provider=self.env_provider,
            hub=self.hub,
        )
        self.workers[agent.name] = worker
        if self.start_workers:
            self.tasks[agent.name] = asyncio.create_task(
                worker.run(), name=f"worker:{agent.name}"
            )
        return worker

    def add(self, agent: AgentConfig, extra_tags: list[str] | None = None) -> AgentWorker:
        """Register an agent and start it. Raises RegistryError on a name clash."""
        self.registry.add(agent, extra_tags)
        return self.start(agent)

    def reconfigure(self, agent: AgentConfig) -> None:
        """Give an existing agent new settings, keeping its queue and worker.

        A run already in flight keeps the settings it was spawned with — its
        command line was built before this call. The next job off the queue uses
        the new ones.
        """
        self.registry.replace(agent)
        worker = self.workers.get(agent.name)
        if worker is not None:
            worker.agent = agent

    async def remove(self, name: str) -> None:
        task = self.tasks.pop(name, None)
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.workers.pop(name, None)
        self.dispatcher.remove(name)
        self.registry.remove(name)

    def worker(self, name: str) -> AgentWorker:
        return self.workers[name]

    async def shutdown(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.tasks.clear()
