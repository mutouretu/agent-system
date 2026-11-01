"""Injection boundaries. No model client or host-code execution is hidden here."""

from typing import Any, Protocol
from rd_platform.agents.base import AgentRole
from .schemas import CoderArtifacts, Contract, SandboxTestResult, Text


class GenerationRequest(Contract):
    role: AgentRole
    system_prompt: Text
    input_data: dict[str, Any]
    output_schema: dict[str, Any]


class ArtifactGenerator(Protocol):
    async def generate(self, request: GenerationRequest) -> dict[str, Any]:
        """Return one JSON artifact object; never execute candidate code."""
        ...


class SandboxExecutor(Protocol):
    async def run_tests(self, code: CoderArtifacts) -> SandboxTestResult:
        """Execute only in Docker; collect exit status, counts and real evidence.

        Implementations own Docker lifecycle, bounded logs, offline dependencies,
        and cleanup. runtime.test_executor provides the Python/pytest implementation.
        """
        ...
