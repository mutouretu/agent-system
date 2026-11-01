from rd_platform.agents.base import REVIEWER, AgentInput, AgentOutput
from rd_platform.domain import AgentMetadata
from rd_platform.manager.agent_registry import register_agent
from .common import GeneratingAgent, failed, input_payload, resolve
from .schemas import ArchitectArtifacts, CoderArtifacts, ReviewDecision, ReviewerArtifacts, TesterArtifacts, code_digest


@register_agent("reviewer")
class ReviewerAgent(GeneratingAgent):
    def __init__(self, generator=None, *, provider=None):
        super().__init__(AgentMetadata("reviewer", "Reviewer", {"review"}, role=REVIEWER), generator, provider=provider)

    async def run(self, request: AgentInput) -> AgentOutput:
        try:
            payload = input_payload(request, {"architecture", "code", "test_evidence"})
            architecture = resolve(request, payload, "architecture", ArchitectArtifacts)
            code = resolve(request, payload, "code", CoderArtifacts)
            data = {"architecture": architecture.model_dump(), "code": code.model_dump()}
            if "test_evidence" in payload or "test_evidence" in payload.get("artifact_sources", {}):
                evidence = resolve(request, payload, "test_evidence", TesterArtifacts)
                if evidence.evidence_logs.code_digest != code_digest(code):
                    raise ValueError("test evidence does not match the reviewed code")
                data["test_evidence"] = evidence.model_dump()
            if self.generator is None:
                return self.unavailable()
            decision = await self.generate(data, ReviewDecision)
            files = {**code.files, **code.tests}
            for item in (*decision.issues, *decision.correction_directives):
                if item.file is not None and item.file not in files:
                    raise ValueError("review references a file not present in the code artifact")
            for issue in decision.issues:
                if issue.line is not None and issue.line > max(1, len(files[issue.file].splitlines())):
                    raise ValueError("review line number exceeds file length")
            result = ReviewerArtifacts(**decision.model_dump(), code_digest=code_digest(code))
            # REJECTED is a successfully produced review, not an execution failure.
            return AgentOutput(artifacts=result.model_dump())
        except Exception as exc:
            return failed(exc)
