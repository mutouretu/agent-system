"""Task contracts and explicit, retryable execution state."""

from dataclasses import dataclass, field
from enum import Enum
import json
from uuid import uuid4


def json_object(value: dict) -> dict:
    """Validate and detach JSON data so agents cannot share mutable payloads."""
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("payload must contain finite JSON values") from exc


class TaskStatus(str, Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


@dataclass
class TaskInput:
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        self.data = json_object(self.data)


@dataclass
class TaskOutput:
    data: dict = field(default_factory=dict)

    def __post_init__(self):
        self.data = json_object(self.data)


@dataclass
class Task:
    name: str
    capability: str
    id: str = field(default_factory=lambda: uuid4().hex)
    input: TaskInput = field(default_factory=TaskInput)
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    output: TaskOutput | None = None
    error: str | None = None
    agent_id: str | None = None

    def __post_init__(self):
        if any(not isinstance(value, str) or not value.strip() for value in (self.id, self.name, self.capability)):
            raise ValueError("task id, name and capability must be nonempty strings")
        self.depends_on = list(self.depends_on)
        if any(not isinstance(value, str) or not value.strip() for value in self.depends_on):
            raise ValueError("dependencies must be nonempty task IDs")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("duplicate task dependency")
        if not isinstance(self.input, TaskInput) or (self.output is not None and not isinstance(self.output, TaskOutput)):
            raise ValueError("input/output must use TaskInput/TaskOutput")
        self.status = TaskStatus(self.status)

    def reset(self) -> None:
        """Explicit retry; dispatch never implicitly repeats terminal tasks."""
        if self.status == TaskStatus.RUNNING:
            raise ValueError("cannot reset a running task")
        self.status = TaskStatus.PENDING
        self.output = None
        self.error = None
        self.agent_id = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "name": self.name, "capability": self.capability,
            "input": json_object(self.input.data), "depends_on": list(self.depends_on),
            "status": self.status.value,
            "output": json_object(self.output.data) if self.output is not None else None,
            "error": self.error, "agent_id": self.agent_id,
        }
