import asyncio
import json

import pytest

from examples.software_team_demo import decode_artifact
from rd_platform.agents.dev.schemas import SandboxTestResult, code_digest
from rd_platform.manager.software_team import SoftwareTeamWorkflow
from rd_platform.runtime.test_executor import DockerTestExecutor, RUNNER, parse_junit, read_report


ARCHITECTURE = {
    "architecture_spec": {"modules": [{"name": "cache", "responsibility": "cache values"}], "data_flow": ["get -> value"]},
    "api_contracts": {"cache": {"type": "object"}},
    "subtasks": [{"id": "code", "description": "implement", "acceptance_criteria": ["works"]}],
}
CODE = {"files": {"src/cache.py": "class Cache: pass\n"}, "tests": {"tests/test_cache.py": "def test_cache(): assert True\n"},
        "commit_message": "implement"}
REJECTED = {"review_status": "REJECTED", "issues": [{"file": "src/cache.py", "severity": "ERROR",
    "message": "incomplete", "suggestion": "implement operations"}],
    "correction_directives": [{"file": "src/cache.py", "instruction": "implement operations"}]}


class FakeGenerator:
    def __init__(self, reject_first=False, reject_always=False):
        self.calls = []
        self.reject_first, self.reject_always = reject_first, reject_always

    async def generate(self, request):
        self.calls.append(request)
        if request.role.value == "ARCHITECT":
            return ARCHITECTURE
        if request.role.value == "CODER":
            return CODE
        reviews = sum(call.role.value == "REVIEWER" for call in self.calls)
        if self.reject_always or (self.reject_first and reviews == 1) or "test_evidence" in request.input_data:
            return REJECTED
        return {"review_status": "APPROVED"}


class FakeExecutor:
    def __init__(self, fail_first=False):
        self.calls = 0
        self.fail_first = fail_first

    async def run_tests(self, code):
        self.calls += 1
        failure = self.fail_first and self.calls == 1
        return SandboxTestResult(code_digest=code_digest(code), execution_mode="fake", executed=True,
            run_id=f"fake-{self.calls}", exit_code=1 if failure else 0,
            counts={"passed": 0 if failure else 1, "failed": 1 if failure else 0, "errors": 0, "skipped": 0},
            logs="simulated assertion failure" if failure else "simulated pass")


def test_successful_workflow_records_all_roles(tmp_path):
    generator, executor = FakeGenerator(), FakeExecutor()
    result = asyncio.run(SoftwareTeamWorkflow(generator, executor, tmp_path).run("implement cache"))
    assert result["status"] == "PASS" and result["iteration_count"] == 0
    assert result["execution_mode"] == "fake" and executor.calls == 1
    assert [call.role.value for call in generator.calls] == ["ARCHITECT", "CODER", "REVIEWER"]
    project = json.loads((tmp_path / "project.json").read_text())
    assert all(task["status"] == "COMPLETED" for task in project["tasks"])


def test_resume_reuses_architecture_and_code_then_reviews_and_tests(tmp_path):
    generator, executor = FakeGenerator(), FakeExecutor()
    events = []

    async def on_event(kind, task):
        events.append((kind, task['id']))

    seed = {'parent_run_id': 'original', 'target_task_id': 'reviewer-00', 'tasks': [
        {'id': 'architect', 'status': 'COMPLETED', 'output': ARCHITECTURE},
        {'id': 'coder-00', 'status': 'COMPLETED', 'output': CODE}]}
    result = asyncio.run(SoftwareTeamWorkflow(generator, executor, tmp_path, on_event=on_event)
                         .run('implement cache', resume_seed=seed))
    assert result['status'] == 'PASS' and result['resumed_from'] == 'original'
    assert [call.role.value for call in generator.calls] == ['REVIEWER']
    assert executor.calls == 1
    assert events[:2] == [('task_finished', 'architect'), ('task_finished', 'coder-00')]
    project = json.loads((tmp_path / 'project.json').read_text())
    assert [task['id'] for task in project['tasks']] == ['architect', 'coder-00', 'reviewer-00', 'tester-00']


@pytest.mark.parametrize('target,expected_roles', [
    ('architect', ['ARCHITECT', 'CODER', 'REVIEWER']),
    ('coder-00', ['CODER', 'REVIEWER']),
    ('reviewer-00', ['REVIEWER']),
    ('tester-00', []),
])
def test_resume_at_any_first_iteration_role(tmp_path, target, expected_roles):
    from rd_platform.agents.dev.schemas import CoderArtifacts, ReviewerArtifacts
    review = ReviewerArtifacts(review_status='APPROVED', code_digest=code_digest(CoderArtifacts.model_validate(CODE))).model_dump()
    saved = [
        {'id': 'architect', 'status': 'COMPLETED', 'output': ARCHITECTURE},
        {'id': 'coder-00', 'status': 'COMPLETED', 'output': CODE},
        {'id': 'reviewer-00', 'status': 'COMPLETED', 'output': review},
    ]
    seed = {'parent_run_id': 'original', 'target_task_id': target,
            'tasks': saved[:[item['id'] for item in saved].index(target)] if target != 'tester-00' else saved}
    generator, executor = FakeGenerator(), FakeExecutor()
    result = asyncio.run(SoftwareTeamWorkflow(generator, executor, tmp_path).run('cache', resume_seed=seed))
    assert result['status'] == 'PASS' and executor.calls == 1
    assert [call.role.value for call in generator.calls] == expected_roles


def test_review_rejection_revises_before_testing(tmp_path):
    generator, executor = FakeGenerator(reject_first=True), FakeExecutor()
    result = asyncio.run(SoftwareTeamWorkflow(generator, executor, tmp_path).run("implement cache"))
    assert result["status"] == "PASS" and result["iteration_count"] == 1 and executor.calls == 1
    assert result["attempts"][0]["tester_status"] == "BLOCKED"
    coder_calls = [call for call in generator.calls if call.role.value == "CODER"]
    assert coder_calls[1].input_data["review"]["review_status"] == "REJECTED"
    assert coder_calls[1].input_data["previous_code"]["files"] == CODE["files"]


def test_test_failure_goes_directly_to_coder_and_repairs(tmp_path):
    generator, executor = FakeGenerator(), FakeExecutor(fail_first=True)
    result = asyncio.run(SoftwareTeamWorkflow(generator, executor, tmp_path).run("implement cache"))
    assert result["status"] == "PASS" and result["iteration_count"] == 1 and executor.calls == 2
    assert result["pass_rates"] == [0, 100] and result["monotonic_nondecreasing"] is True
    coder_calls = [call for call in generator.calls if call.role.value == "CODER"]
    assert len(coder_calls) == 2
    assert coder_calls[1].input_data["test_evidence"]["test_summary"]["outcome"] == "FAILED"
    assert "simulated assertion failure" in coder_calls[1].input_data["test_evidence"]["evidence_logs"]["logs"]
    assert [call.role.value for call in generator.calls].count("REVIEWER") == 2


def test_revision_limit_cannot_be_bypassed(tmp_path):
    executor = FakeExecutor()
    result = asyncio.run(SoftwareTeamWorkflow(FakeGenerator(reject_always=True), executor, tmp_path, max_retries=2).run("cache"))
    assert result["status"] == "FAILED" and result["iteration_count"] == 2
    assert len(result["attempts"]) == 3 and executor.calls == 0


@pytest.mark.parametrize("maximum", [-1, 4, True])
def test_invalid_retry_limit(tmp_path, maximum):
    with pytest.raises(ValueError):
        SoftwareTeamWorkflow(FakeGenerator(), FakeExecutor(), tmp_path, max_retries=maximum)


def test_generator_failure_stops_workflow(tmp_path):
    class Broken(FakeGenerator):
        async def generate(self, request):
            raise RuntimeError("unavailable")

    executor = FakeExecutor()
    result = asyncio.run(SoftwareTeamWorkflow(Broken(), executor, tmp_path).run("cache"))
    assert result["status"] == "FAILED" and executor.calls == 0
    assert "unavailable" in result["error"]


def test_junit_counts_use_testcases_not_untrusted_header():
    raw = b'<testsuites tests="999"><testsuite><testcase/><testcase><failure/></testcase><testcase><error/></testcase><testcase><skipped/></testcase></testsuite></testsuites>'
    assert parse_junit(raw).model_dump() == {"passed": 1, "failed": 1, "errors": 1, "skipped": 1}


@pytest.mark.parametrize("raw", [b'<!DOCTYPE x><testsuite/>', b'<other/>', b'<testsuite><testcase><error/><failure/></testcase></testsuite>'])
def test_invalid_junit_is_rejected(raw):
    with pytest.raises(ValueError):
        parse_junit(raw)


def test_report_reader_refuses_symlinks(tmp_path):
    outside = tmp_path / "actual.xml"
    outside.write_text("<testsuite/>")
    link = tmp_path / "linked.xml"
    link.symlink_to(outside)
    with pytest.raises(OSError):
        read_report(link)


def test_runner_disables_untrusted_pytest_configuration():
    assert '"--noconftest"' in RUNNER and '"/input/pytest.ini"' in RUNNER
    assert 'os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"' in RUNNER
    # Compiling is allowed here because this pytest itself is container-only.
    compile(RUNNER, "trusted-runner", "exec")


@pytest.mark.parametrize("path", ["../bad.py", "src/bad.sh", "tests/other.py"])
def test_executor_restricts_python_source_paths(path):
    with pytest.raises(ValueError):
        DockerTestExecutor._python_path(path, "src/")


def test_cli_envelope_is_data_only():
    source = "raise RuntimeError('never execute on host')"
    assert decode_artifact(json.dumps({"artifact_json": json.dumps({"source": source})})) == {"source": source}
    with pytest.raises(ValueError):
        decode_artifact('{"artifact_json":"[]"}')
