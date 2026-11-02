"""Mixed backend dispatch is independent of live model credentials."""

import asyncio
from types import SimpleNamespace

import pytest

from rd_platform.domain.agent import AgentRole
from rd_platform.manager.role_router import RoleRouter
from rd_platform.agents.dev.interfaces import GenerationRequest


class FakeGenerator:
    def __init__(self, name):
        self.name = name
        self.calls = []
        self.requests = []

    async def generate(self, request):
        self.requests.append(request)
        self.calls.append({"role": request.role.value, "status": "COMPLETED"})
        return {"from": self.name}


def test_mixed_backend_routes_roles_and_records_origin():
    codex = FakeGenerator("codex")
    deepseek = FakeGenerator("deepseek")
    router = RoleRouter(codex, deepseek)

    async def run():
        return [await router.generate(SimpleNamespace(role=role)) for role in
                (AgentRole.ARCHITECT, AgentRole.CODER, AgentRole.REVIEWER)]

    assert asyncio.run(run()) == [{"from": "codex"}, {"from": "deepseek"}, {"from": "deepseek"}]
    assert [call["backend"] for call in router.calls] == ["codex", "llm", "llm"]
    with pytest.raises(ValueError, match="no model generator"):
        asyncio.run(router.generate(SimpleNamespace(role=AgentRole.TESTER)))


def test_configured_roles_use_separate_models_and_project_responsibilities():
    architect, coder, reviewer = (FakeGenerator(name) for name in ("architect", "coder", "reviewer"))
    router = RoleRouter(architect, coder, reviewer,
        backends={AgentRole.ARCHITECT: "codex", AgentRole.CODER: "llm", AgentRole.REVIEWER: "llm"},
        responsibilities={AgentRole.CODER: "实现并测试缓存逻辑"})
    request = GenerationRequest(role=AgentRole.CODER, system_prompt="Base contract",
        input_data={}, output_schema={})
    assert asyncio.run(router.generate(request)) == {"from": "coder"}
    assert "实现并测试缓存逻辑" in coder.requests[0].system_prompt
    assert request.system_prompt == "Base contract"
    assert reviewer.requests == []
