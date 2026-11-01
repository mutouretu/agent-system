"""Framework entities, independent of HTTP and model SDKs."""

from .agent import AgentMetadata
from .project import Project
from .task import Task, TaskInput, TaskOutput, TaskStatus

__all__ = ["AgentMetadata", "Project", "Task", "TaskInput", "TaskOutput", "TaskStatus"]
