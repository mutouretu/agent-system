"""Declarative metadata for trusted, registered agent implementations."""

from dataclasses import dataclass, field

from .task import json_object


@dataclass(frozen=True)
class AgentMetadata:
    id: str
    name: str
    capabilities: frozenset[str]
    tools: tuple[str, ...] = ()
    config: dict = field(default_factory=dict)

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (self.id, self.name)):
            raise ValueError("agent id and name must be nonempty strings")
        if isinstance(self.capabilities, str) or not self.capabilities:
            raise ValueError("capabilities must be a nonempty collection")
        if isinstance(self.tools, str):
            raise ValueError("tools must be a collection")
        capabilities, tools = frozenset(self.capabilities), tuple(self.tools)
        if any(not isinstance(value, str) or not value.strip() for value in (*capabilities, *tools)):
            raise ValueError("capabilities and tools must be nonempty strings")
        object.__setattr__(self, "capabilities", capabilities)
        object.__setattr__(self, "tools", tools)
        object.__setattr__(self, "config", json_object(self.config))

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "capabilities": sorted(self.capabilities),
                "tools": list(self.tools), "config": json_object(self.config)}
