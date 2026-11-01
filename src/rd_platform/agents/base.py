"""Trusted agent interface; untrusted code/tools must use DockerSandbox."""

from abc import ABC, abstractmethod
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator

from rd_platform.domain import AgentMetadata, AgentRole, Task, TaskInput, TaskStatus
from rd_platform.domain.task import json_object

ARCHITECT = AgentRole.ARCHITECT
CODER = AgentRole.CODER
REVIEWER = AgentRole.REVIEWER
TESTER = AgentRole.TESTER


class AgentStatus(str, Enum):
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class AgentInput(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    task_id: StrictStr = Field(min_length=1)
    task_name: StrictStr = ""
    capability: StrictStr = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    context: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[StrictStr] = Field(default_factory=list)

    @field_validator("task_id")
    @classmethod
    def nonblank_id(cls, value):
        if not value.strip():
            raise ValueError("task_id must not be blank")
        return value

    @field_validator("payload", "context", mode="before")
    @classmethod
    def finite_json(cls, value):
        return json_object(value)

    @classmethod
    def from_task(cls, task: Task, context: dict):
        return cls(task_id=task.id, task_name=task.name, capability=task.capability,
                   payload=task.input.data, context=context, depends_on=list(task.depends_on))


class AgentOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    status: AgentStatus = AgentStatus.COMPLETED
    artifacts: dict[str, Any] = Field(default_factory=dict)
    logs: StrictStr | None = Field(default=None, max_length=65536)
    error: StrictStr | None = Field(default=None, max_length=4096)

    @field_validator("artifacts", mode="before")
    @classmethod
    def finite_json(cls, value):
        return json_object(value)


class BaseAgent(ABC):
    def __init__(self, metadata: AgentMetadata):
        self.metadata = metadata

    @abstractmethod
    async def run(self, request: AgentInput) -> AgentOutput:
        """Return structured status, artifacts and optional logs.

        Implementations are registered by trusted application code, never loaded
        from request strings. Dynamic scripts, tests and shell require a sandbox.
        """
        raise NotImplementedError


class LegacyAgentAdapter(BaseAgent):
    """Explicit bridge for trusted run(task, context) -> dict implementations."""

    def __init__(self, legacy):
        if not isinstance(getattr(legacy, "metadata", None), AgentMetadata):
            raise ValueError("legacy agent requires AgentMetadata")
        super().__init__(legacy.metadata)
        self.legacy = legacy

    async def run(self, request: AgentInput) -> AgentOutput:
        task = Task(id=request.task_id, name=request.task_name or request.task_id,
                    capability=request.capability or sorted(self.metadata.capabilities)[0],
                    input=TaskInput(request.payload), depends_on=list(request.depends_on),
                    status=TaskStatus.RUNNING, agent_id=self.metadata.id)
        result = await self.legacy.run(task, json_object(request.context))
        return AgentOutput(artifacts=json_object(result))
