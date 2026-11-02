"""Route structured generation requests to trusted, role-specific backends."""

from rd_platform.domain.agent import AgentRole


class RoleRouter:
    def __init__(self, architect, implementation, reviewer=None, *, backends=None, responsibilities=None):
        self.architect = architect
        self.implementation = implementation
        self.reviewer = reviewer or implementation
        self.backends = backends or {AgentRole.ARCHITECT: "codex", AgentRole.CODER: "llm", AgentRole.REVIEWER: "llm"}
        self.responsibilities = responsibilities or {}
        self.calls = []

    async def generate(self, request):
        generators = {AgentRole.ARCHITECT: self.architect, AgentRole.CODER: self.implementation,
                      AgentRole.REVIEWER: self.reviewer}
        if request.role not in generators:
            raise ValueError("this role has no model generator")
        generator, backend = generators[request.role], self.backends[request.role]
        duty = self.responsibilities.get(request.role)
        if duty:
            request = request.model_copy(update={"system_prompt": request.system_prompt + "\nProject role responsibility: " + duty})
        before = len(generator.calls)
        try:
            return await generator.generate(request)
        finally:
            if len(generator.calls) > before:
                self.calls.append({"backend": backend, **generator.calls[-1]})
