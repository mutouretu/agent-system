"""Trusted agent interface; untrusted code/tools must use DockerSandbox."""

from abc import ABC, abstractmethod

from rd_platform.domain import AgentMetadata, Task


class BaseAgent(ABC):
    def __init__(self, metadata: AgentMetadata):
        self.metadata = metadata

    @abstractmethod
    async def run(self, task: Task, context: dict) -> dict:
        """Return JSON output. Context contains detached direct dependency outputs.

        Implementations are registered by trusted application code, never loaded
        from request strings. Dynamic scripts, tests and shell require a sandbox.
        """
        raise NotImplementedError
