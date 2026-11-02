from rd_platform.agents.base import CODER, AgentInput, AgentOutput
from rd_platform.domain import AgentMetadata
from rd_platform.manager.agent_registry import register_agent
from .common import GeneratingAgent, failed, input_payload, resolve
from .schemas import ArchitectArtifacts, CoderArtifacts, ReviewerArtifacts, TesterArtifacts, Text, Contract, code_digest


class CoderInput(Contract):
    architecture: ArchitectArtifacts
    instructions: Text | None = None
    previous_code: CoderArtifacts | None = None
    review: ReviewerArtifacts | None = None
    test_evidence: TesterArtifacts | None = None
    operator_guidance: Text | None = None


@register_agent("coder")
class CoderAgent(GeneratingAgent):
    def __init__(self, generator=None, *, provider=None):
        super().__init__(AgentMetadata("coder", "Coder", {"coding"}, role=CODER), generator, provider=provider)

    async def run(self, request: AgentInput) -> AgentOutput:
        try:
            payload = input_payload(request, {"architecture", "instructions", "previous_code", "review", "test_evidence", "operator_guidance"})
            values = {"architecture": resolve(request, payload, "architecture", ArchitectArtifacts),
                      "instructions": payload.get("instructions"),
                      "operator_guidance": payload.get("operator_guidance")}
            for key, schema in (("previous_code", CoderArtifacts), ("review", ReviewerArtifacts)):
                if key in payload or key in payload.get("artifact_sources", {}):
                    values[key] = resolve(request, payload, key, schema)
            if "test_evidence" in payload:
                values["test_evidence"] = TesterArtifacts.model_validate(payload["test_evidence"])
            inputs = CoderInput.model_validate(values)
            if inputs.review is not None:
                if inputs.previous_code is None or inputs.review.code_digest != code_digest(inputs.previous_code):
                    raise ValueError("correction review must match previous_code")
                failed_test = inputs.test_evidence is not None and inputs.test_evidence.test_summary.outcome in ("FAILED", "ERROR")
                if inputs.review.review_status != "REJECTED" and not failed_test:
                    raise ValueError("correction requires a REJECTED review or failed test evidence")
            if inputs.test_evidence is not None:
                if inputs.previous_code is None or inputs.test_evidence.evidence_logs.code_digest != code_digest(inputs.previous_code):
                    raise ValueError("test evidence must match previous_code")
                if inputs.test_evidence.test_summary.outcome not in ("FAILED", "ERROR"):
                    raise ValueError("test evidence must contain a failed test outcome")
            if self.generator is None:
                return self.unavailable()
            result = await self.generate(inputs.model_dump(), CoderArtifacts, repair_format=True)
            if inputs.previous_code is not None:
                # A repair response is a set of complete file replacements.
                # Omitted paths retain their previous tested candidate content.
                result = CoderArtifacts(
                    files={**inputs.previous_code.files, **result.files},
                    tests={**inputs.previous_code.tests, **result.tests},
                    commit_message=result.commit_message)
            return AgentOutput(artifacts=result.model_dump())
        except Exception as exc:
            return failed(exc)
