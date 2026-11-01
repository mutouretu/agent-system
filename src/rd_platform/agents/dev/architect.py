from pydantic import Field
from rd_platform.agents.base import ARCHITECT, AgentInput, AgentOutput
from rd_platform.domain import AgentMetadata
from rd_platform.manager.agent_registry import register_agent
from .common import GeneratingAgent, failed, input_payload
from .schemas import ArchitectArtifacts, Contract, Text


class ArchitectInput(Contract):
    requirements: Text
    constraints: dict = Field(default_factory=dict)


@register_agent("architect")
class ArchitectAgent(GeneratingAgent):
    def __init__(self, generator=None, *, provider=None):
        super().__init__(AgentMetadata("architect", "Architect", {"architecture"}, role=ARCHITECT), generator, provider=provider)

    async def run(self, request: AgentInput) -> AgentOutput:
        try:
            payload = input_payload(request, {"requirements", "constraints"})
            payload.pop("artifact_sources", None)
            inputs = ArchitectInput.model_validate(payload)
            if self.generator is None:
                return self.unavailable()
            result = await self.generate(inputs.model_dump(), ArchitectArtifacts)
            return AgentOutput(artifacts=result.model_dump())
        except Exception as exc:
            return failed(exc)
