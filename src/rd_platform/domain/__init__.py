"""Framework entities, independent of HTTP and model SDKs."""

from .agent import AgentMetadata, AgentRole
from .project import Project
from .task import Task, TaskInput, TaskOutput, TaskStatus

__all__ = ["AgentMetadata", "AgentRole", "Project", "Task", "TaskInput", "TaskOutput", "TaskStatus"]
