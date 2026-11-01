"""Process-local capability matching, deterministic by registration order."""

from rd_platform.agents.base import BaseAgent
from rd_platform.domain import AgentMetadata


class AgentNotFound(LookupError):
    pass


class AgentRegistry:
    def __init__(self):
        self._agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        if not isinstance(agent, BaseAgent) or not isinstance(agent.metadata, AgentMetadata):
            raise ValueError("register a BaseAgent with AgentMetadata")
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
