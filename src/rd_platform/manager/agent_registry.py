"""Trusted class catalog and process-local capability matching."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from rd_platform.domain import AgentMetadata

if TYPE_CHECKING:
    from rd_platform.agents.base import BaseAgent

AGENT_REGISTRY: dict[str, type[BaseAgent]] = {}


def _validate_class(agent_class):
    # Lazy type import avoids a cycle when built-ins register at import time.
    from rd_platform.agents.base import BaseAgent

    if not inspect.isclass(agent_class) or not issubclass(agent_class, BaseAgent) or inspect.isabstract(agent_class):
        raise ValueError("agent implementation must be a concrete BaseAgent class")
    if not inspect.iscoroutinefunction(agent_class.run):
        raise ValueError("agent run must be async")
    try:
        inspect.signature(agent_class.run).bind(None, None)
    except TypeError as exc:
        raise ValueError("run must accept one AgentInput; wrap old agents with LegacyAgentAdapter") from exc


def register_agent(name: str):
    """Register a zero-argument implementation class; never instantiate at import."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("agent registration name must be nonempty")

    def decorate(agent_class):
        _validate_class(agent_class)
        if name in AGENT_REGISTRY:
            raise ValueError(f"agent implementation already registered: {name}")
        AGENT_REGISTRY[name] = agent_class
        return agent_class

    return decorate


class AgentNotFound(LookupError):
    pass


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        from rd_platform.agents.base import BaseAgent

        if not isinstance(agent, BaseAgent) or not isinstance(agent.metadata, AgentMetadata):
            raise ValueError("register a BaseAgent with AgentMetadata")
        _validate_class(type(agent))
        if agent.metadata.id in self._agents:
            raise ValueError(f"agent already registered: {agent.metadata.id}")
        self._agents[agent.metadata.id] = agent

    def unregister(self, agent_id: str) -> BaseAgent:
        return self._agents.pop(agent_id)

    def find_by_capability(self, capability: str) -> list[BaseAgent]:
        return [agent for agent in self._agents.values() if capability in agent.metadata.capabilities]

    def resolve(self, capability: str) -> BaseAgent:
        matches = self.find_by_capability(capability)
        if not matches:
            raise AgentNotFound(f"no registered agent supports capability: {capability}")
        return matches[0]

    def list_agents(self) -> list[AgentMetadata]:
        return [agent.metadata for agent in self._agents.values()]

    @classmethod
    def from_catalog(cls, catalog=None) -> AgentRegistry:
        """Create fresh instances from a snapshot; existing registries stay isolated."""
        registry = cls()
        for name, agent_class in dict(AGENT_REGISTRY if catalog is None else catalog).items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("agent registration name must be nonempty")
            _validate_class(agent_class)
            try:
                agent = agent_class()
            except TypeError as exc:
                raise ValueError(f"catalog agent {name} requires a zero-argument constructor; register a configured instance instead") from exc
            registry.register(agent)
        return registry


def create_default_registry() -> AgentRegistry:
    """Shared defaults for the API and trusted in-container Python clients."""
    # Only trusted built-ins are imported. External plugins must be explicitly
    # imported by application bootstrap code before creating the registry.
    from rd_platform.agents import impl  # noqa: F401

    return AgentRegistry.from_catalog()
