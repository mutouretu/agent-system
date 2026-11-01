"""Generic, model-free protocol and dynamic-registration tests (Docker only)."""

import asyncio

import pytest
from pydantic import ValidationError

from rd_platform.agents import (
    ARCHITECT, CODER, REVIEWER, TESTER, AgentInput, AgentOutput,
    AgentRole, AgentStatus, BaseAgent, LegacyAgentAdapter,
)
from rd_platform.domain import AgentMetadata, Project, Task, TaskInput, TaskStatus
from rd_platform.manager import AGENT_REGISTRY, AgentRegistry, Dispatcher, create_default_registry, register_agent


@pytest.fixture(autouse=True)
def isolated_catalog():
    previous = dict(AGENT_REGISTRY)
    AGENT_REGISTRY.clear()
    try:
        yield
    finally:
        AGENT_REGISTRY.clear()
        AGENT_REGISTRY.update(previous)


class EchoAgent(BaseAgent):
    def __init__(self):
        super().__init__(AgentMetadata("echo", "Echo", {"echo"}, role=CODER))
        self.calls = 0

    async def run(self, request: AgentInput) -> AgentOutput:
        self.calls += 1
        return AgentOutput(artifacts={"echo": request.payload}, logs=f"echo task {request.task_id}")


class DummyAgent(BaseAgent):
    def __init__(self):
        super().__init__(AgentMetadata("dummy", "Dummy", {"review"}, role=REVIEWER))

    async def run(self, request: AgentInput) -> AgentOutput:
        return AgentOutput(artifacts={"reviewed": request.context["dependencies"]}, logs="review complete")


def test_roles_and_json_schema():
    assert {role.value for role in AgentRole} == {"ARCHITECT", "CODER", "REVIEWER", "TESTER"}
    assert (ARCHITECT, CODER, REVIEWER, TESTER) == tuple(AgentRole)
    assert EchoAgent().metadata.to_dict()["role"] == "CODER"
    assert {"task_id", "context", "payload"} <= AgentInput.model_json_schema()["properties"].keys()
    assert {"status", "artifacts", "logs"} <= AgentOutput.model_json_schema()["properties"].keys()
    output = AgentOutput(artifacts={"file": {"path": "src/main.py", "content": "print(1)"}})
    assert AgentOutput.model_validate_json(output.model_dump_json()) == output


def test_abstract_base_and_decorator_registration():
    with pytest.raises(TypeError):
        BaseAgent(AgentMetadata("base", "Base", {"base"}))
    assert register_agent("echo")(EchoAgent) is EchoAgent
    assert AGENT_REGISTRY == {"echo": EchoAgent}
    registry = create_default_registry()
    assert isinstance(registry.resolve("echo"), EchoAgent)
    task = Task("echo", "echo", id="task", input=TaskInput({"message": "hello"}))
    asyncio.run(Dispatcher(registry).dispatch(task))
    assert task.status == TaskStatus.COMPLETED
    assert task.output.data == {"echo": {"message": "hello"}}
    assert task.logs == "echo task task"


def test_dictionary_registration_dag_and_context_propagation():
    AGENT_REGISTRY.update(echo=EchoAgent, dummy=DummyAgent)
    first = Task("code", "echo", id="code", input=TaskInput({"files": ["src/main.py"]}))
    second = Task("review", "review", depends_on=["code"])
    project = Project("team", tasks=[second, first])
    asyncio.run(Dispatcher(AgentRegistry.from_catalog()).dispatch(project))
    assert first.status == second.status == TaskStatus.COMPLETED
    assert second.output.data == {"reviewed": {"code": {"echo": {"files": ["src/main.py"]}}}}
    assert second.to_dict()["logs"] == "review complete"


def test_fresh_instances_and_catalog_snapshot():
    register_agent("echo")(EchoAgent)
    first, second = create_default_registry(), create_default_registry()
    assert first.resolve("echo") is not second.resolve("echo")
    asyncio.run(Dispatcher(first).dispatch(Task("hello", "echo")))
    assert first.resolve("echo").calls == 1 and second.resolve("echo").calls == 0
    register_agent("dummy")(DummyAgent)
    assert first.find_by_capability("review") == []
    assert len(create_default_registry().find_by_capability("review")) == 1
    first.unregister("echo")
    assert "echo" in AGENT_REGISTRY  # Removing an instance doesn't edit the catalog.


def test_duplicate_and_invalid_registration():
    register_agent("echo")(EchoAgent)
    with pytest.raises(ValueError, match="already registered"):
        register_agent("echo")(DummyAgent)
    with pytest.raises(ValueError, match="nonempty"):
        register_agent(" ")
    with pytest.raises(ValueError, match="concrete BaseAgent"):
        register_agent("invalid")(object)
    with pytest.raises(ValueError, match="concrete BaseAgent"):
        AgentRegistry.from_catalog({"invalid": object})


def test_sync_and_legacy_signatures_require_explicit_adapter():
    class SyncAgent(EchoAgent):
        def run(self, request):
            return AgentOutput()

    class OldAgent(EchoAgent):
        async def run(self, task, context):
            return {"id": task.id, "payload": task.input.data}

    with pytest.raises(ValueError, match="async"):
        register_agent("sync")(SyncAgent)
    with pytest.raises(ValueError, match="LegacyAgentAdapter"):
        AgentRegistry().register(OldAgent())
    registry = AgentRegistry()
    registry.register(LegacyAgentAdapter(OldAgent()))
    task = Task("old", "echo", id="old-task", input=TaskInput({"v": 1}))
    asyncio.run(Dispatcher(registry).dispatch(task))
    assert task.status == TaskStatus.COMPLETED and task.output.data == {"id": "old-task", "payload": {"v": 1}}


def test_configured_instance_can_be_registered_without_zero_argument_constructor():
    class ConfiguredAgent(EchoAgent):
        def __init__(self, dependency):
            super().__init__()
            self.dependency = dependency

    register_agent("configured")(ConfiguredAgent)
    with pytest.raises(ValueError, match="zero-argument constructor"):
        AgentRegistry.from_catalog()
    registry = AgentRegistry()
    registry.register(ConfiguredAgent("fake"))
    assert registry.resolve("echo").dependency == "fake"


@pytest.mark.parametrize("payload", [
    {"task_id": ""}, {"task_id": " "}, {"task_id": 42},
    {"task_id": "t", "context": []}, {"task_id": "t", "payload": {"bad": float("nan")}},
    {"task_id": "t", "extra": True}, {"task_id": "t", "context": {"bad": object()}},
])
def test_invalid_inputs_rejected(payload):
    with pytest.raises(ValidationError):
        AgentInput(**payload)


@pytest.mark.parametrize("payload", [
    {"status": "RUNNING"}, {"status": "unknown"}, {"artifacts": []},
    {"artifacts": {"bad": float("inf")}}, {"logs": 123}, {"logs": "x" * 65537},
    {"extra": 1},
])
def test_invalid_outputs_rejected(payload):
    with pytest.raises(ValidationError):
        AgentOutput(**payload)


def test_inputs_are_detached_and_defaults_are_not_shared():
    class MutatingAgent(EchoAgent):
        async def run(self, request):
            request.payload["nested"]["v"] = 2
            request.context["nested"]["v"] = 3
            request.depends_on.append("fake")
            return AgentOutput(artifacts=request.payload)

    registry = AgentRegistry()
    registry.register(MutatingAgent())
    task = Task("mutate", "echo", input=TaskInput({"nested": {"v": 1}}))
    context = {"nested": {"v": 1}}
    asyncio.run(Dispatcher(registry).dispatch(task, context))
    assert task.input.data == context == {"nested": {"v": 1}}
    assert task.depends_on == [] and task.output.data == {"nested": {"v": 2}}
    one, two = AgentInput(task_id="a"), AgentInput(task_id="b")
    one.payload["x"] = 1
    assert two.payload == {}


@pytest.mark.parametrize("status", [AgentStatus.FAILED, AgentStatus.BLOCKED])
def test_business_failure_preserves_evidence_and_blocks_downstream(status):
    class FailingAgent(EchoAgent):
        async def run(self, request):
            return AgentOutput(status=status, artifacts={"report": "rejected"}, logs="check failed", error="contract mismatch")

    registry = AgentRegistry()
    registry.register(FailingAgent())
    registry.register(DummyAgent())
    task = Task("code", "echo", id="code")
    downstream = Task("review", "review", depends_on=["code"])
    asyncio.run(Dispatcher(registry).dispatch(Project("fail", tasks=[task, downstream])))
    assert task.status.value == status.value and downstream.status == TaskStatus.BLOCKED
    assert task.output.data == {"report": "rejected"}
    assert task.error == "contract mismatch" and task.logs == "check failed"
    task.reset()
    assert task.logs is task.output is task.error is None


@pytest.mark.parametrize("kind", ["dict", "mutation", "constructed"])
def test_dispatcher_revalidates_agent_output(kind):
    class InvalidAgent(EchoAgent):
        async def run(self, request):
            if kind == "dict":
                return {"status": "COMPLETED", "artifacts": {}}
            if kind == "constructed":
                return AgentOutput.model_construct(status=AgentStatus.COMPLETED, artifacts={"bad": object()})
            output = AgentOutput()
            output.artifacts["bad"] = float("nan")
            return output

    registry = AgentRegistry()
    registry.register(InvalidAgent())
    task = Task("bad", "echo")
    asyncio.run(Dispatcher(registry).dispatch(task))
    assert task.status == TaskStatus.FAILED and task.output is None


def test_new_protocol_timeout_and_cancellation():
    async def scenario():
        started = asyncio.Event()

        class WaitingAgent(EchoAgent):
            async def run(self, request):
                started.set()
                await asyncio.Event().wait()

        registry = AgentRegistry()
        registry.register(WaitingAgent())
        task = Task("wait", "echo")
        await Dispatcher(registry, task_timeout=0.01).dispatch(task)
        assert task.status == TaskStatus.FAILED and task.error.startswith("TimeoutError")
        task.reset()
        started.clear()
        active = asyncio.create_task(Dispatcher(registry).dispatch(task))
        await asyncio.wait_for(started.wait(), 1)
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert task.status == TaskStatus.FAILED and "CancelledError" in task.error

    asyncio.run(scenario())
