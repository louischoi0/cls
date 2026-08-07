"""Loading and validation of agents.yaml.

Every problem here aborts startup. A registry that is wrong in a way we only
notice at dispatch time is worse than a server that refuses to boot.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .models import AgentConfig


class RegistryError(Exception):
    """agents.yaml is missing, malformed, or internally inconsistent."""


class Registry:
    def __init__(self, agents: list[AgentConfig]) -> None:
        if not agents:
            raise RegistryError("agents.yaml defines no agents")

        self.by_name: dict[str, AgentConfig] = {}
        for agent in agents:
            if agent.name in self.by_name:
                raise RegistryError(f"duplicate agent name {agent.name!r}")
            self.by_name[agent.name] = agent

        # tag -> agent names, including each agent's own name as an implicit tag
        self._by_tag: dict[str, list[str]] = {}
        for agent in agents:
            for tag in [agent.name, *agent.tags]:
                self._by_tag.setdefault(tag, []).append(agent.name)

    @property
    def names(self) -> list[str]:
        return list(self.by_name)

    def all_agents(self) -> list[AgentConfig]:
        return list(self.by_name.values())

    def match_tag(self, tag: str) -> list[str]:
        """Agent names whose name or tag list contains `tag`."""
        return list(self._by_tag.get(tag, ()))


def load_registry(path: Path) -> Registry:
    if not path.is_file():
        raise RegistryError(f"agents file not found: {path}")

    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise RegistryError(f"{path}: invalid YAML: {exc}") from exc

    if not isinstance(raw, dict) or "agents" not in raw:
        raise RegistryError(f"{path}: expected a top-level 'agents:' list")
    entries = raw["agents"]
    if not isinstance(entries, list):
        raise RegistryError(f"{path}: 'agents' must be a list")

    agents: list[AgentConfig] = []
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise RegistryError(f"{path}: agents[{i}] must be a mapping")
        try:
            agent = AgentConfig(**entry)
        except ValidationError as exc:
            raise RegistryError(f"{path}: agents[{i}] invalid: {exc}") from exc
        if not agent.cwd.is_dir():
            raise RegistryError(
                f"{path}: agent {agent.name!r} cwd does not exist: {agent.cwd}"
            )
        agents.append(agent)

    return Registry(agents)
