"""Project DAG validation and deterministic readiness evaluation."""

from collections import deque
from dataclasses import dataclass, field
from uuid import uuid4

from .task import Task, TaskStatus, json_object


@dataclass
class Project:
    name: str
    id: str = field(default_factory=lambda: uuid4().hex)
    context: dict = field(default_factory=dict)
    tasks: list[Task] = field(default_factory=list)

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (self.id, self.name)):
            raise ValueError("project id and name must be nonempty strings")
        self.context = json_object(self.context)
        initial = list(self.tasks)
        self.tasks = []
        for task in initial:
            self.add_task(task)

    def add_task(self, task: Task) -> None:
        """Forward references are allowed while constructing the project."""
        if not isinstance(task, Task):
            raise ValueError("project tasks must be Task instances")
        if any(existing.id == task.id for existing in self.tasks):
            raise ValueError(f"duplicate task ID: {task.id}")
        self.tasks.append(task)

    def _graph(self):
        by_id = {task.id: task for task in self.tasks}
        if len(by_id) != len(self.tasks):
            raise ValueError("duplicate task ID")
        children = {task_id: [] for task_id in by_id}
        degrees = {}
        for task in self.tasks:
            if len(set(task.depends_on)) != len(task.depends_on):
                raise ValueError(f"duplicate dependency in task {task.id}")
            degrees[task.id] = len(task.depends_on)
            for dependency in task.depends_on:
                if dependency not in by_id:
                    raise ValueError(f"task {task.id} has unknown dependency {dependency}")
                children[dependency].append(task.id)
        ready = deque(task_id for task_id, degree in degrees.items() if degree == 0)
        order = []
        while ready:
            task_id = ready.popleft()
            order.append(by_id[task_id])
            for child in children[task_id]:
                degrees[child] -= 1
                if degrees[child] == 0:
                    ready.append(child)
        if len(order) != len(self.tasks):
            raise ValueError("task dependencies contain a cycle")
        return children, order

    def build_dag(self) -> dict[str, list[str]]:
        """Return parent -> children adjacency; reject cycles/dangling IDs."""
        return self._graph()[0]

    def topological_tasks(self) -> list[Task]:
        return self._graph()[1]

    def get_executable_tasks(self) -> list[Task]:
        order = self.topological_tasks()
        by_id = {task.id: task for task in order}
        return [task for task in order if task.status == TaskStatus.PENDING and all(
            by_id[dependency].status == TaskStatus.COMPLETED for dependency in task.depends_on
        )]

    def to_dict(self) -> dict:
        return {"id": self.id, "name": self.name, "context": json_object(self.context),
                "tasks": [task.to_dict() for task in self.tasks]}
