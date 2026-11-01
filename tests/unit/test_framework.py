"""Model-free framework tests. Execute pytest inside Docker only."""

import asyncio

import pytest

from rd_platform.agents import AlgorithmRunnerAgent, BaseAgent, DataPipelineAgent
from rd_platform.domain import AgentMetadata, Project, Task, TaskInput, TaskOutput, TaskStatus
from rd_platform.manager import AgentNotFound, AgentRegistry, DispatchConflict, Dispatcher


def registry():
    result = AgentRegistry()
    result.register(DataPipelineAgent())
    result.register(AlgorithmRunnerAgent())
    return result


def pipeline():
    first = Task("prepare", "data_processing", id="prepare", input=TaskInput({"values": [3, 1, 3, 2]}))
    second = Task("calculate", "algorithm_execution", id="calculate", depends_on=[first.id])
    # Forward reference and reverse insertion order exercise real DAG ordering.
    return Project("pipeline", context={"tenant": "demo"}, tasks=[second, first]), first, second


def test_project_dag_and_readiness():
    project, first, second = pipeline()
    assert project.build_dag() == {second.id: [], first.id: [second.id]}
    assert project.topological_tasks() == [first, second]
    assert project.get_executable_tasks() == [first]
    first.status = TaskStatus.COMPLETED
    first.output = TaskOutput({"values": [1, 2, 3]})
    assert project.get_executable_tasks() == [second]
    with pytest.raises(ValueError, match="duplicate"):
        project.add_task(first)


@pytest.mark.parametrize("dependencies", [["unknown"], ["self"]])
def test_invalid_dag_rejected_before_execution(dependencies):
    task = Task("bad", "data_processing", id="self", depends_on=dependencies)
    with pytest.raises(ValueError):
        asyncio.run(Dispatcher(registry()).dispatch(Project("bad", tasks=[task])))
    assert task.status == TaskStatus.PENDING


def test_multiple_node_cycle():
    project = Project("cycle", tasks=[
        Task("a", "x", id="a", depends_on=["b"]), Task("b", "x", id="b", depends_on=["a"]),
    ])
    with pytest.raises(ValueError, match="cycle"):
        project.build_dag()


def test_registry_matching_registration_and_removal():
    agents = registry()
    assert isinstance(agents.resolve("data_processing"), DataPipelineAgent)
    assert len(agents.find_by_capability("algorithm_execution")) == 1
    assert agents.find_by_capability("unknown") == []
    with pytest.raises(ValueError, match="already registered"):
        agents.register(DataPipelineAgent())
    agents.unregister("data-pipeline")
    with pytest.raises(AgentNotFound):
        agents.resolve("data_processing")


def test_builtin_dag_dispatch_and_idempotency():
    project, first, second = pipeline()
    dispatcher = Dispatcher(registry())
    assert asyncio.run(dispatcher.dispatch(project)) is project
    assert all(task.status == TaskStatus.COMPLETED for task in project.tasks)
    assert first.output.data == {"values": [1, 2, 3], "count": 3}
    assert second.output.data == {"count": 3, "sum": 6, "min": 1, "max": 3, "mean": 2.0}
    before = project.to_dict()
    asyncio.run(dispatcher.dispatch(project))
    assert project.to_dict() == before
    assert first.agent_id == "data-pipeline" and second.agent_id == "algorithm-runner"


def test_failure_blocks_descendants_but_independent_branch_completes():
    project, first, second = pipeline()
    first.input = TaskInput({"values": [True]})
    last = Task("last", "algorithm_execution", depends_on=[second.id])
    independent = Task("independent", "algorithm_execution", input=TaskInput({"values": []}))
    project.add_task(last)
    project.add_task(independent)
    asyncio.run(Dispatcher(registry()).dispatch(project))
    assert first.status == TaskStatus.FAILED
    assert "ValueError" in first.error
    assert second.status == last.status == TaskStatus.BLOCKED
    assert independent.status == TaskStatus.COMPLETED
    assert independent.output.data["mean"] is None


def test_missing_agent_and_explicit_retry():
    project, first, second = pipeline()
    agents = AgentRegistry()
    dispatcher = Dispatcher(agents)
    asyncio.run(dispatcher.dispatch(project))
    assert first.status == second.status == TaskStatus.BLOCKED
    agents.register(DataPipelineAgent())
    agents.register(AlgorithmRunnerAgent())
    for task in project.tasks:
        task.reset()
    asyncio.run(dispatcher.dispatch(project))
    assert all(task.status == TaskStatus.COMPLETED for task in project.tasks)


class InspectAgent(BaseAgent):
    def __init__(self):
        super().__init__(AgentMetadata("inspect", "Inspect", {"inspect"}))
        self.seen = []

    async def run(self, task, context):
        self.seen.append((task.id, context["dependencies"].copy()))
        assert task.status == TaskStatus.RUNNING
        task.depends_on.clear()
        context["nested"]["changed"] = True
        return {"value": task.id}


def test_dependency_outputs_and_context_are_detached():
    agents = AgentRegistry()
    agent = InspectAgent()
    agents.register(agent)
    first = Task("first", "inspect", id="first")
    second = Task("second", "inspect", id="second", depends_on=["first"])
    project = Project("context", context={"nested": {"changed": False}}, tasks=[second, first])
    asyncio.run(Dispatcher(agents).dispatch(project))
    assert agent.seen == [("first", {}), ("second", {"first": {"value": "first"}})]
    assert project.context == {"nested": {"changed": False}}
    assert second.depends_on == ["first"]


def test_standalone_task_and_unresolved_dependency():
    dispatcher = Dispatcher(registry())
    task = Task("solo", "algorithm_execution", input=TaskInput({"values": [4, 8]}))
    asyncio.run(dispatcher.dispatch(task))
    assert task.output.data["mean"] == 6
    dependent = Task("dependent", "data_processing", depends_on=[task.id])
    with pytest.raises(ValueError, match="requires its Project"):
        asyncio.run(dispatcher.dispatch(dependent))
    assert dependent.status == TaskStatus.PENDING


def test_waiting_task_remains_pending():
    project, _, second = pipeline()
    asyncio.run(Dispatcher(registry()).dispatch_task(second, project=project))
    assert second.status == TaskStatus.PENDING


@pytest.mark.parametrize("result", [[1], {"bad": object()}, {"bad": float("nan")}])
def test_invalid_agent_output_fails(result):
    class BadAgent(BaseAgent):
        async def run(self, task, context):
            return result

    agents = AgentRegistry()
    agents.register(BadAgent(AgentMetadata("bad", "bad", {"bad"})))
    task = Task("bad", "bad")
    asyncio.run(Dispatcher(agents).dispatch(task))
    assert task.status == TaskStatus.FAILED and task.output is None
    assert task.error.startswith("ValueError:")


def test_concurrent_dispatch_and_cancellation_release_project():
    async def scenario():
        started = asyncio.Event()

        class SlowAgent(BaseAgent):
            async def run(self, task, context):
                started.set()
                await asyncio.Event().wait()

        agents = AgentRegistry()
        agents.register(SlowAgent(AgentMetadata("slow", "slow", {"slow"})))
        task = Task("slow", "slow")
        project = Project("slow", tasks=[task])
        dispatcher = Dispatcher(agents)
        active = asyncio.create_task(dispatcher.dispatch(project))
        await asyncio.wait_for(started.wait(), 1)
        with pytest.raises(DispatchConflict):
            await dispatcher.dispatch(project)
        with pytest.raises(DispatchConflict):
            await dispatcher.dispatch(task)
        with pytest.raises(ValueError, match="running"):
            task.reset()
        active.cancel()
        with pytest.raises(asyncio.CancelledError):
            await active
        assert task.status == TaskStatus.FAILED
        assert "CancelledError" in task.error
        await dispatcher.dispatch(project)  # Lock was released; terminal task isn't retried.

    asyncio.run(scenario())


def test_task_timeout():
    class SlowAgent(BaseAgent):
        async def run(self, task, context):
            await asyncio.Event().wait()

    agents = AgentRegistry()
    agents.register(SlowAgent(AgentMetadata("slow", "slow", {"slow"})))
    task = Task("slow", "slow")
    asyncio.run(Dispatcher(agents, task_timeout=0.01).dispatch(task))
    assert task.status == TaskStatus.FAILED
    assert task.error.startswith("TimeoutError:")


def test_join_requires_explicit_source_for_builtin_algorithm():
    first = Task("first", "data_processing", id="first", input=TaskInput({"values": [1, 3]}))
    second = Task("second", "data_processing", id="second", input=TaskInput({"values": [8]}))
    join = Task("join", "algorithm_execution", depends_on=["first", "second"], input=TaskInput({"source_task": "first"}))
    project = Project("join", tasks=[join, first, second])
    asyncio.run(Dispatcher(registry()).dispatch(project))
    assert join.output.data["mean"] == 2
