from rd_platform.agents.base import CODER, AgentInput, AgentOutput
from rd_platform.domain import AgentMetadata
from rd_platform.manager.agent_registry import register_agent
from .common import GeneratingAgent, failed, input_payload, resolve
from .schemas import ArchitectArtifacts, CoderArtifacts, ReviewerArtifacts, Text, Contract, code_digest


class CoderInput(Contract):
    architecture: ArchitectArtifacts
    instructions: Text | None = None
    previous_code: CoderArtifacts | None = None
    review: ReviewerArtifacts | None = None


@register_agent("coder")
class CoderAgent(GeneratingAgent):
    def __init__(self, generator=None, *, provider=None):
        super().__init__(AgentMetadata("coder", "Coder", {"coding"}, role=CODER), generator, provider=provider)

    async def run(self, request: AgentInput) -> AgentOutput:
        try:
            payload = input_payload(request, {"architecture", "instructions", "previous_code", "review"})
            values = {"architecture": resolve(request, payload, "architecture", ArchitectArtifacts),
                      "instructions": payload.get("instructions")}
            for key, schema in (("previous_code", CoderArtifacts), ("review", ReviewerArtifacts)):
                if key in payload or key in payload.get("artifact_sources", {}):
                    values[key] = resolve(request, payload, key, schema)
            inputs = CoderInput.model_validate(values)
            if inputs.review is not None:
                if inputs.previous_code is None or inputs.review.code_digest != code_digest(inputs.previous_code):
                    raise ValueError("correction review must match previous_code")
                if inputs.review.review_status != "REJECTED":
                    raise ValueError("correction input requires a REJECTED review")
            if self.generator is None:
                return self.unavailable()
            result = await self.generate(inputs.model_dump(), CoderArtifacts)
            return AgentOutput(artifacts=result.model_dump())
        except Exception as exc:
            return failed(exc)
