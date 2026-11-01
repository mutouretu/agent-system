"""Single-process async DAG execution, with bounded task duration and retries.

This is an in-memory coordinator, not a durable queue or a security sandbox.
"""

import asyncio
import math

from rd_platform.agents.base import AgentInput, AgentOutput
from rd_platform.domain import Project, Task, TaskOutput, TaskStatus
from rd_platform.domain.task import json_object
from .agent_registry import AgentNotFound, AgentRegistry


class DispatchConflict(RuntimeError):
    pass


class Dispatcher:
    def __init__(self, registry: AgentRegistry, *, task_timeout: float = 30):
        if not math.isfinite(task_timeout) or task_timeout <= 0:
            raise ValueError("task_timeout must be positive and finite")
        self.registry = registry
        self.task_timeout = task_timeout
        self._projects: set[str] = set()

    async def dispatch(self, target: Project | Task, context: dict | None = None) -> Project | Task:
        if isinstance(target, Project):
            return await self.dispatch_project(target, context)
        if isinstance(target, Task):
            return await self.dispatch_task(target, context)
        raise TypeError("dispatch expects a Project or Task")

    async def dispatch_task(self, task: Task, context: dict | None = None, *, project: Project | None = None) -> Task:
        if task.status == TaskStatus.RUNNING:
            raise DispatchConflict(f"task is already running: {task.id}")
        if task.status != TaskStatus.PENDING:
            return task
        execution_context = json_object(context if context is not None else {})
        dependencies = {}
        if task.depends_on:
            if project is None:
                raise ValueError("dependent task requires its Project")
            project.build_dag()
            by_id = {item.id: item for item in project.tasks}
            if by_id.get(task.id) is not task:
                raise ValueError("task must belong to the supplied project")
            upstream = [by_id[task_id] for task_id in task.depends_on]
            failed = [item.id for item in upstream if item.status in (TaskStatus.FAILED, TaskStatus.BLOCKED)]
            if failed:
                task.status = TaskStatus.BLOCKED
                task.error = f"dependencies failed or blocked: {', '.join(failed)}"
                return task
            if any(item.status != TaskStatus.COMPLETED for item in upstream):
                return task
            if any(item.output is None for item in upstream):
                raise ValueError("completed dependencies must have output")
            dependencies = {item.id: json_object(item.output.data) for item in upstream}
        execution_context["dependencies"] = dependencies
        if project is not None:
            execution_context["project_id"] = project.id
        try:
            agent = self.registry.resolve(task.capability)
        except AgentNotFound as exc:
            task.status = TaskStatus.BLOCKED
            task.error = str(exc)
            return task
        task.status = TaskStatus.RUNNING
        task.error = None
        task.output = None
        task.logs = None
        task.agent_id = agent.metadata.id
        try:
            request = AgentInput.from_task(task, execution_context)
            async with asyncio.timeout(self.task_timeout):
                result = await agent.run(request)
            if not isinstance(result, AgentOutput):
                raise ValueError("agent run must return AgentOutput")
            # Revalidate even model_construct() or mutated nested artifact data.
            result = AgentOutput.model_validate(result.model_dump())
            task.output = TaskOutput(result.artifacts)
            task.status = TaskStatus(result.status.value)
            task.logs = result.logs
            if task.status != TaskStatus.COMPLETED:
                task.error = (result.error or result.logs or f"agent returned {task.status.value}")[:4096]
        except asyncio.CancelledError:
            task.status = TaskStatus.FAILED
            task.error = "CancelledError: dispatch cancelled"
            raise
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = f"{type(exc).__name__}: {exc}"[:4096]
        return task

    async def dispatch_project(self, project: Project, context: dict | None = None) -> Project:
        if project.id in self._projects or any(task.status == TaskStatus.RUNNING for task in project.tasks):
            raise DispatchConflict(f"project is already running: {project.id}")
        order = project.topological_tasks()  # Validate before any task changes.
        execution_context = json_object(project.context)
        if context is not None:
            execution_context.update(json_object(context))
        self._projects.add(project.id)
        try:
            for task in order:
                await self.dispatch_task(task, execution_context, project=project)
        finally:
            self._projects.remove(project.id)
        return project
