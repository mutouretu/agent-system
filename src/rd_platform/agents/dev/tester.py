from rd_platform.agents.base import TESTER, AgentInput, AgentOutput, AgentStatus, BaseAgent
from rd_platform.domain import AgentMetadata
from rd_platform.manager.agent_registry import register_agent
from .common import failed, input_payload, resolve
from .interfaces import SandboxExecutor
from .schemas import CoderArtifacts, EvidenceLogs, ReviewerArtifacts, SandboxTestResult, TesterArtifacts, TestSummary, code_digest


@register_agent("tester")
class TesterAgent(BaseAgent):
    def __init__(self, executor: SandboxExecutor | None = None):
        super().__init__(AgentMetadata("tester", "Tester", {"testing"}, role=TESTER))
        self.executor = executor

    @staticmethod
    def not_run(digest, reason):
        artifacts = TesterArtifacts(test_summary=TestSummary(outcome="NOT_RUN"),
                                    evidence_logs=EvidenceLogs(code_digest=digest, execution_mode="not_run", logs=reason))
        return AgentOutput(status=AgentStatus.BLOCKED, artifacts=artifacts.model_dump(), error=reason)

    async def run(self, request: AgentInput) -> AgentOutput:
        try:
            payload = input_payload(request, {"code", "review"})
            code = resolve(request, payload, "code", CoderArtifacts)
            review = resolve(request, payload, "review", ReviewerArtifacts)
            digest = code_digest(code)
            if review.code_digest != digest:
                return self.not_run(digest, "review does not match the current code")
            if review.review_status != "APPROVED":
                return self.not_run(digest, "review must be APPROVED before testing")
            if self.executor is None:
                return self.not_run(digest, "sandbox executor is not configured")
            receipt = await self.executor.run_tests(code.model_copy(deep=True))
            if not isinstance(receipt, SandboxTestResult):
                raise ValueError("executor must return SandboxTestResult")
            receipt = SandboxTestResult.model_validate(receipt.model_dump())
            if receipt.code_digest != digest:
                raise ValueError("executor evidence does not match the submitted code")
            if not receipt.executed:
                return self.not_run(digest, receipt.logs or "executor did not run tests")
            counts = receipt.counts
            success = receipt.exit_code == 0 and counts is not None and counts.passed > 0 and not (counts.failed or counts.errors or counts.skipped)
            summary = TestSummary(outcome="PASSED" if success else ("ERROR" if counts is None else "FAILED"),
                                  total=counts.total if counts is not None else None, counts=counts,
                                  pass_rate=100 * counts.passed / counts.total if counts is not None and counts.total else None,
                                  coverage=receipt.coverage)
            evidence = EvidenceLogs(code_digest=digest, execution_mode=receipt.execution_mode, run_id=receipt.run_id,
                                    exit_code=receipt.exit_code, logs=receipt.logs)
            artifacts = TesterArtifacts(test_summary=summary, evidence_logs=evidence)
            return AgentOutput(status=AgentStatus.COMPLETED if success else AgentStatus.FAILED,
                               artifacts=artifacts.model_dump(), logs=receipt.logs,
                               error=None if success else "test suite did not fully pass")
        except Exception as exc:
            return failed(exc)
