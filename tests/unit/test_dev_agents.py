"""Phase-A contracts with explicit fake backends; no generated code is executed."""

import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from rd_platform.agents import AgentInput, AgentStatus, ARCHITECT, CODER, REVIEWER, TESTER
from rd_platform.agents import dev
from rd_platform.agents.dev import schemas as S
from rd_platform.domain import Project, Task, TaskInput, TaskStatus
from rd_platform.manager import AgentRegistry, AGENT_REGISTRY, Dispatcher


def architecture():
    return {
        "architecture_spec": {"modules": [{"name": "cache", "responsibility": "bounded key/value storage"}],
                              "data_flow": ["get/put -> cache -> result"]},
        "api_contracts": {"cache": {"type": "object", "properties": {"capacity": {"type": "integer", "minimum": 1}}}},
        "subtasks": [{"id": "implement", "description": "implement bounded storage",
                      "acceptance_criteria": ["least recently used key is evicted"]}],
    }


def code():
    # This fixture is an artifact, not a claim that a generated implementation was tested.
    return {"files": {"src/cache.py": "class Cache: pass\n"},
            "tests": {"tests/test_cache.py": "def test_placeholder(): assert True\n"},
            "commit_message": "feat: implement cache"}


def rejected():
    return {"review_status": "REJECTED", "issues": [{"file": "src/cache.py", "line": 1, "severity": "ERROR",
            "message": "missing operations", "suggestion": "implement get and put"}],
            "correction_directives": [{"file": "src/cache.py", "instruction": "implement get and put with eviction"}]}


def review(approved=True, candidate=None):
    decision = {"review_status": "APPROVED"} if approved else rejected()
    return S.ReviewerArtifacts(**decision, code_digest=S.code_digest(S.CoderArtifacts.model_validate(candidate or code()))).model_dump()


class FakeGenerator:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def generate(self, request):
        self.calls.append(request.model_copy(deep=True))
        if isinstance(self.result, Exception):
            raise self.result
        return deepcopy(self.result)


class FakeExecutor:
    def __init__(self, **overrides):
        self.calls = 0
        self.overrides = overrides

    async def run_tests(self, candidate):
        self.calls += 1
        return S.SandboxTestResult(**{
            "code_digest": S.code_digest(candidate), "execution_mode": "fake", "executed": True,
            "run_id": "fake-run", "exit_code": 0,
            "counts": {"passed": 2, "failed": 0, "errors": 0, "skipped": 0},
            "logs": "SIMULATED executor receipt; no candidate tests were executed", **self.overrides,
        })


def run(agent, payload=None, dependencies=None):
    request = AgentInput(task_id="test", payload=payload or {}, context={"dependencies": dependencies or {}})
    return asyncio.run(agent.run(request))


def test_four_roles_registered_and_schemas_exported():
    for name, cls, role in (("architect", dev.ArchitectAgent, ARCHITECT), ("coder", dev.CoderAgent, CODER),
                            ("reviewer", dev.ReviewerAgent, REVIEWER), ("tester", dev.TesterAgent, TESTER)):
        assert AGENT_REGISTRY[name] is cls
        assert cls().metadata.role == role
    for schema in (S.ArchitectArtifacts, S.CoderArtifacts, S.ReviewerArtifacts, S.TesterArtifacts):
        assert schema.model_json_schema()["additionalProperties"] is False


def test_architect_structured_prompt_and_output():
    backend = FakeGenerator(architecture())
    result = run(dev.ArchitectAgent(backend), {"requirements": "Implement a bounded cache", "constraints": {"language": "Python"}})
    assert result.status == AgentStatus.COMPLETED
    S.ArchitectArtifacts.model_validate(result.artifacts)
    call = backend.calls[0]
    assert call.role == ARCHITECT
    assert call.input_data["requirements"] == "Implement a bounded cache"
    assert "Do not invoke shell/tools" in call.system_prompt
    assert "architecture_spec" in call.output_schema["properties"]


def test_coder_consumes_upstream_and_does_not_execute_code():
    candidate = code()
    candidate["files"]["src/cache.py"] = "raise RuntimeError('must never execute during generation')\n"
    backend = FakeGenerator(candidate)
    result = run(dev.CoderAgent(backend), dependencies={"design": architecture()})
    assert result.status == AgentStatus.COMPLETED
    assert result.artifacts["files"] == candidate["files"]
    assert backend.calls[0].role == CODER
    assert backend.calls[0].input_data["architecture"]["subtasks"][0]["id"] == "implement"


def test_coder_recovers_flat_test_path_and_missing_summary_without_fabricating_code():
    candidate = {"files": {"src/cache.py": "class Cache: pass\n"},
                 "`tests/test_cache.py`": "def test_cache(): assert True\n"}
    result = run(dev.CoderAgent(FakeGenerator(candidate)), {"architecture": architecture()})
    assert result.status == AgentStatus.COMPLETED
    assert result.artifacts["tests"] == {"tests/test_cache.py": candidate["`tests/test_cache.py`"]}
    assert result.artifacts["commit_message"] == "Implement requested changes"
    assert "`tests/test_cache.py`" in candidate  # source input is unchanged


def test_coder_rejects_missing_real_tests_and_unknown_fields(caplog):
    for candidate in ({"files": {"src/cache.py": "pass\n"}},
                      {"files": {"src/cache.py": "pass\n"}, "tests": {}, "diff": ""}):
        result = run(dev.CoderAgent(FakeGenerator(candidate)), {"architecture": architecture()})
        assert result.status == AgentStatus.FAILED and result.artifacts == {}
    assert "agent_artifact_rejected role=CODER" in caplog.text


def test_coder_retries_schema_failure_with_field_feedback():
    class RepairGenerator:
        def __init__(self):
            self.calls = []

        async def generate(self, request):
            self.calls.append(request)
            return {'files': {'src/cache.py': 'class Cache: pass\n'}} if len(self.calls) == 1 else code()

    generator = RepairGenerator()
    result = run(dev.CoderAgent(generator), {'architecture': architecture()})
    assert result.status == AgentStatus.COMPLETED
    assert len(generator.calls) == 2
    assert generator.calls[1].input_data['format_feedback'][0]['path'] == 'tests'


@pytest.mark.parametrize("path", ["/tmp/pwn", "../pwn", "src/../../pwn", "src//file.py", "./file.py",
                                  "C:/file.py", "src\\file.py", ".git/config", ".ssh/id_rsa", "src/\x00.py"])
def test_unsafe_artifact_paths_fail_before_delivery(path):
    candidate = code()
    candidate["files"] = {path: "pass"}
    result = run(dev.CoderAgent(FakeGenerator(candidate)), {"architecture": architecture()})
    assert result.status == AgentStatus.FAILED and result.artifacts == {}


@pytest.mark.parametrize("files,tests", [
    ({"src/x.py": "a"}, {"src/x.py": "b"}), ({"src/X.py": "a"}, {"src/x.py": "b"}),
    ({"src": "a"}, {"src/test.py": "b"}), ({"a.py": "a"}, {"test_a.py": " "}),
])
def test_source_test_collisions_and_empty_tests_rejected(files, tests):
    with pytest.raises(ValidationError):
        S.CoderArtifacts(files=files, tests=tests, commit_message="change")


def test_artifact_size_limits():
    candidate = code()
    candidate["files"] = {"large.py": "a" * (256 * 1024 + 1)}
    with pytest.raises(ValidationError):
        S.CoderArtifacts.model_validate(candidate)


@pytest.mark.parametrize("dependency", ["missing", "implement"])
def test_invalid_architecture_dag_rejected(dependency):
    data = architecture()
    data["subtasks"][0]["depends_on"] = [dependency]
    result = run(dev.ArchitectAgent(FakeGenerator(data)), {"requirements": "build"})
    assert result.status == AgentStatus.FAILED


def test_missing_and_ambiguous_inputs_do_not_call_backend():
    backend = FakeGenerator(code())
    assert run(dev.CoderAgent(backend)).status == AgentStatus.FAILED
    assert run(dev.CoderAgent(backend), dependencies={"a": architecture(), "b": architecture()}).status == AgentStatus.FAILED
    assert backend.calls == []
    result = run(dev.CoderAgent(backend), {"artifact_sources": {"architecture": "a"}}, {"a": architecture(), "b": architecture()})
    assert result.status == AgentStatus.COMPLETED
    assert run(dev.CoderAgent(backend), {"artifact_sources": {"architecture": "missing"}}, {"a": architecture()}).status == AgentStatus.FAILED


def test_rejected_review_is_completed_and_bound_to_exact_code():
    backend = FakeGenerator(rejected())
    result = run(dev.ReviewerAgent(backend), dependencies={"design": architecture(), "implementation": code()})
    assert result.status == AgentStatus.COMPLETED
    assert result.artifacts["review_status"] == "REJECTED"
    assert result.artifacts["code_digest"] == S.code_digest(S.CoderArtifacts.model_validate(code()))
    assert backend.calls[0].role == REVIEWER
    assert "code_digest" not in backend.calls[0].output_schema["properties"]


@pytest.mark.parametrize("decision", [
    {"review_status": "REJECTED"},
    {**rejected(), "review_status": "APPROVED"},
    {"review_status": "APPROVED", "correction_directives": [{"instruction": "fix"}]},
    {**rejected(), "issues": [{**rejected()["issues"][0], "file": "unknown.py"}]},
    {**rejected(), "issues": [{**rejected()["issues"][0], "line": 200}]},
])
def test_invalid_review_decisions_and_locations_fail(decision):
    result = run(dev.ReviewerAgent(FakeGenerator(decision)), {"architecture": architecture(), "code": code()})
    assert result.status == AgentStatus.FAILED


def test_reviewer_repairs_one_structural_json_error_without_weakening_contract():
    class RepairGenerator:
        def __init__(self): self.calls=[]
        async def generate(self, request):
            self.calls.append(request)
            if len(self.calls)==1:
                return {'review_status':'REJECTED', 'issues':[{'severity':'ERROR','message':'missing bound'}],
                        'correction_directives':[{'instruction':'add bound'}]}
            return rejected()
    backend=RepairGenerator()
    result=run(dev.ReviewerAgent(backend), {'architecture':architecture(),'code':code()})
    assert result.status == AgentStatus.COMPLETED
    assert len(backend.calls)==2
    assert backend.calls[1].input_data['format_feedback'][0]['path'].endswith('suggestion')


def test_reviewer_format_repair_is_bounded_and_never_forges_approval():
    class AlwaysMalformed:
        def __init__(self): self.calls=[]
        async def generate(self, request):
            self.calls.append(request)
            return {'review_status':'REJECTED',
                    'issues':[{'severity':'ERROR','message':'missing bound', 'long unexpected key '*200:'bad'}],
                    'correction_directives':[{'instruction':'add bound'}]}
    backend=AlwaysMalformed()
    result=run(dev.ReviewerAgent(backend), {'architecture':architecture(),'code':code()})
    assert result.status == AgentStatus.FAILED
    assert len(backend.calls)==3
    assert len(backend.calls[1].input_data['format_feedback'][0]['path'])<=180
    assert all('format_feedback' in call.input_data for call in backend.calls[1:])


def test_correction_input_validates_previous_revision_without_auto_retry():
    backend = FakeGenerator(code())
    payload = {"architecture": architecture(), "previous_code": code(), "review": review(False)}
    assert run(dev.CoderAgent(backend), payload).status == AgentStatus.COMPLETED
    assert len(backend.calls) == 1
    assert backend.calls[0].input_data["review"]["correction_directives"]
    payload["previous_code"]["files"]["src/cache.py"] = "# changed"
    assert run(dev.CoderAgent(backend), payload).status == AgentStatus.FAILED
    assert len(backend.calls) == 1


def test_coder_accepts_matching_failed_test_evidence_after_approved_review():
    previous = S.CoderArtifacts.model_validate(code())
    digest = S.code_digest(previous)
    evidence = S.TesterArtifacts(
        test_summary=S.TestSummary(outcome='FAILED', total=1,
            counts={'passed': 0, 'failed': 1, 'errors': 0, 'skipped': 0}, pass_rate=0.0),
        evidence_logs=S.EvidenceLogs(code_digest=digest, execution_mode='docker',
            run_id='sandbox-1', exit_code=1, logs='AssertionError: expected 2')).model_dump()
    backend = FakeGenerator(code())
    payload = {'architecture': architecture(), 'previous_code': code(),
               'review': review(True), 'test_evidence': evidence}
    result = run(dev.CoderAgent(backend), payload)
    assert result.status == AgentStatus.COMPLETED
    assert 'AssertionError' in backend.calls[0].input_data['test_evidence']['evidence_logs']['logs']
    payload['test_evidence']['evidence_logs']['code_digest'] = '0' * 64
    assert run(dev.CoderAgent(backend), payload).status == AgentStatus.FAILED
    assert len(backend.calls) == 1


def test_unconfigured_roles_are_blocked_and_tester_does_not_invent_metrics():
    assert run(dev.ArchitectAgent(), {"requirements": "build"}).status == AgentStatus.BLOCKED
    assert run(dev.CoderAgent(), {"architecture": architecture()}).status == AgentStatus.BLOCKED
    assert run(dev.ReviewerAgent(), {"architecture": architecture(), "code": code()}).status == AgentStatus.BLOCKED
    result = run(dev.TesterAgent(), {"code": code(), "review": review()})
    assert result.status == AgentStatus.BLOCKED
    summary = result.artifacts["test_summary"]
    assert summary == {"outcome": "NOT_RUN", "total": None, "counts": None, "pass_rate": None, "coverage": None}


@pytest.mark.parametrize("stale", [False, True])
def test_unapproved_or_stale_review_never_calls_executor(stale):
    executor = FakeExecutor()
    approval = review(stale)
    if stale:
        approval["code_digest"] = "0" * 64
    result = run(dev.TesterAgent(executor), {"code": code(), "review": approval})
    assert result.status == AgentStatus.BLOCKED and executor.calls == 0
    assert result.artifacts["test_summary"]["outcome"] == "NOT_RUN"


@pytest.mark.parametrize("counts,exit_code,outcome,rate", [
    ({"passed": 2, "failed": 0, "errors": 0, "skipped": 0}, 0, "PASSED", 100),
    ({"passed": 1, "failed": 1, "errors": 0, "skipped": 0}, 1, "FAILED", 50),
    ({"passed": 1, "failed": 0, "errors": 0, "skipped": 1}, 0, "FAILED", 50),
    ({"passed": 0, "failed": 0, "errors": 0, "skipped": 0}, 5, "FAILED", None),
    (None, 137, "ERROR", None),
])
def test_tester_metrics_are_derived_from_explicit_fake_receipts(counts, exit_code, outcome, rate):
    executor = FakeExecutor(counts=counts, exit_code=exit_code)
    result = run(dev.TesterAgent(executor), dependencies={"code": code(), "approval": review()})
    assert executor.calls == 1
    assert result.status == (AgentStatus.COMPLETED if outcome == "PASSED" else AgentStatus.FAILED)
    assert result.artifacts["test_summary"]["outcome"] == outcome
    assert result.artifacts["test_summary"]["pass_rate"] == rate
    assert result.artifacts["test_summary"]["coverage"] is None
    assert result.artifacts["evidence_logs"]["execution_mode"] == "fake"


def test_forged_or_inconsistent_executor_receipt_fails():
    for overrides in ({"code_digest": "0" * 64}, {"executed": False}, {"counts": None},
                      {"counts": {"passed": 1, "failed": 1, "errors": 0, "skipped": 0}}):
        result = run(dev.TesterAgent(FakeExecutor(**overrides)), {"code": code(), "review": review()})
        assert result.status == AgentStatus.FAILED


def test_schema_rejects_false_success_and_unmeasured_metrics():
    with pytest.raises(ValidationError):
        S.TestSummary(outcome="NOT_RUN", pass_rate=100)
    with pytest.raises(ValidationError):
        S.TestSummary(outcome="PASSED")
    with pytest.raises(ValidationError):
        S.SandboxTestResult(code_digest="0" * 64, execution_mode="fake", executed=False, coverage=95)


def test_generator_failure_and_prose_are_not_artifacts():
    for response in (RuntimeError("backend unavailable"), "```json {} ```", {"unknown": 1}):
        backend = FakeGenerator(response)
        result = run(dev.ArchitectAgent(backend), {"requirements": "build"})
        assert result.status == AgentStatus.FAILED and result.artifacts == {}
        assert len(backend.calls) == 1


def test_fake_artifact_dag_stops_at_rejected_review_without_running_tests():
    registry = AgentRegistry()
    registry.register(dev.ArchitectAgent(FakeGenerator(architecture())))
    registry.register(dev.CoderAgent(FakeGenerator(code())))
    registry.register(dev.ReviewerAgent(FakeGenerator(rejected())))
    executor = FakeExecutor()
    registry.register(dev.TesterAgent(executor))
    tasks = [Task("design", "architecture", id="design", input=TaskInput({"requirements": "cache"})),
             Task("code", "coding", id="code", depends_on=["design"]),
             Task("review", "review", id="review", depends_on=["design", "code"]),
             Task("test", "testing", depends_on=["code", "review"])]
    asyncio.run(Dispatcher(registry).dispatch(Project("fake-only", tasks=list(reversed(tasks)))))
    assert [task.status for task in tasks] == [TaskStatus.COMPLETED] * 3 + [TaskStatus.BLOCKED]
    assert tasks[2].output.data["review_status"] == "REJECTED" and executor.calls == 0
