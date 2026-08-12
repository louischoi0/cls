"""The live set of sessions, indexed by name.

This used to load `agents.yaml` at startup and abort the boot on anything wrong
with it. Sessions are created and deleted at runtime now, so there is no file to
validate and nothing here can refuse to start — the store is the record, and
this is the in-memory index the dispatcher and the pool share with it.
"""

from __future__ import annotations

from .models import SessionConfig


class RegistryError(Exception):
    """A name that is taken, or one that is not there at all."""


class Registry:
    def __init__(self, sessions: list[SessionConfig] | None = None) -> None:
        self.by_name: dict[str, SessionConfig] = {}
        for session in sessions or []:
            self.add(session)

    def add(self, session: SessionConfig) -> None:
        if session.name in self.by_name:
            raise RegistryError(f"duplicate session name {session.name!r}")
        self.by_name[session.name] = session

    def replace(self, session: SessionConfig) -> None:
        """Swap one session's settings. The name is the identity and cannot move."""
        if session.name not in self.by_name:
            raise RegistryError(f"unknown session {session.name!r}")
        self.by_name[session.name] = session

    def remove(self, name: str) -> None:
        if self.by_name.pop(name, None) is None:
            raise RegistryError(f"unknown session {name!r}")

    def get(self, name: str) -> SessionConfig | None:
        return self.by_name.get(name)

    def all_agents(self) -> list[SessionConfig]:
        return list(self.by_name.values())

    @property
    def names(self) -> list[str]:
        return list(self.by_name)

    def __contains__(self, name: object) -> bool:
        return name in self.by_name

    def __len__(self) -> int:
        return len(self.by_name)
