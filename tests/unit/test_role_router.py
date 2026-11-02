"""Mixed backend dispatch is independent of live model credentials."""

import asyncio
from types import SimpleNamespace

import pytest

from rd_platform.domain.agent import AgentRole
from rd_platform.manager.role_router import RoleRouter


class FakeGenerator:
    def __init__(self, name):
        self.name = name
        self.calls = []

    async def generate(self, request):
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
