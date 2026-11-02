"""Route structured generation requests to trusted, role-specific backends."""

from rd_platform.domain.agent import AgentRole


class RoleRouter:
    def __init__(self, architect, implementation):
        self.architect = architect
        self.implementation = implementation
        self.calls = []

    async def generate(self, request):
        if request.role == AgentRole.ARCHITECT:
            generator, backend = self.architect, "codex"
        elif request.role in (AgentRole.CODER, AgentRole.REVIEWER):
            generator, backend = self.implementation, "llm"
        else:
            raise ValueError("this role has no model generator")
        before = len(generator.calls)
        try:
            return await generator.generate(request)
        finally:
            if len(generator.calls) > before:
                self.calls.append({"backend": backend, **generator.calls[-1]})
