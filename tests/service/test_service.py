"""Run only inside Docker; service extras are optional for runtime-only tests."""

import hashlib
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from rd_platform.service.api import MAX_BYTES, Settings, create_app
from rd_platform.service import probe_api


@pytest.fixture
def service(tmp_path):
    settings = Settings(*(tmp_path / name for name in ("data", "logs", "models")))
    for path in (settings.data, settings.logs, settings.models):
        path.mkdir()
    with TestClient(create_app(settings)) as client:
        yield client, settings


def test_health_and_storage(service):
    client, settings = service
    assert client.get("/health").json()["status"] == "ok"
    payload = {"records": [{"id": "中文", "score": 3, "active": True}]}
    saved = client.put("/v1/data/sample", json=payload)
    assert saved.status_code == 200
    assert saved.json()["sha256"] == hashlib.sha256((settings.data / "sample.json").read_bytes()).hexdigest()
    assert client.get("/v1/data/sample").json()["records"] == payload["records"]
    assert client.get("/v1/data/missing").status_code == 404
    assert (settings.logs / "service.log").stat().st_size > 0


def test_filter_boundaries(service):
    client, _ = service
    payload = {"records": [
        {"id": "b", "score": 5}, {"id": "a", "score": 5},
        {"id": "a", "score": 4}, {"id": "c", "score": 6, "active": False},
    ], "min_score": 5}
    assert client.post("/v1/algorithms/filter", json=payload).json() == {
        "records": [{"id": "a", "score": 5}, {"id": "b", "score": 5}], "count": 2,
    }
    payload["limit"] = 0
    assert client.post("/v1/algorithms/filter", json=payload).json()["records"] == []


@pytest.mark.parametrize("payload", [
    {"records": [{"id": "a", "score": True}]},
    {"records": [{"id": "a", "score": "2"}]},
    {"records": [], "limit": -1}, {"records": [], "min_score": False},
    {"records": [{"id": "a", "score": 2, "active": "true"}]},
])
def test_strict_input(service, payload):
    assert service[0].post("/v1/algorithms/filter", json=payload).status_code == 422


def test_reject_paths_symlinks_and_corrupt_data(service, tmp_path):
    client, settings = service
    assert client.put("/v1/data/bad.name", json={"records": []}).status_code == 422
    outside = tmp_path / "outside.json"
    outside.write_text('{"records":[]}')
    (settings.data / "link.json").symlink_to(outside)
    assert client.get("/v1/data/link").status_code == 422
    # Replacing a symlink replaces its directory entry, never the target.
    assert client.put("/v1/data/link", json={"records": [{"id": "a", "score": 2}]}).status_code == 200
    assert outside.read_text() == '{"records":[]}'
    (settings.data / "broken.json").write_text("invalid")
    assert client.get("/v1/data/broken").status_code == 422


def test_body_limit(service):
    assert service[0].put("/v1/data/big", content=b"x" * (MAX_BYTES + 1)).status_code == 413


def test_models_and_readiness(service):
    client, settings = service
    (settings.models / "weight.bin").write_bytes(b"sample")
    (settings.models / "link").symlink_to(settings.models / "weight.bin")
    assert client.get("/v1/models").json() == {"models": ["weight.bin"], "read_only": True}
    # Workbench persistence now occupies the data mount; simulate losing the mount.
    settings.data.rename(settings.data.with_name("data-unavailable"))
    assert client.get("/health").status_code == 503


def test_missing_mount_fails_startup(tmp_path):
    with pytest.raises(RuntimeError, match="required mount missing"):
        with TestClient(create_app(Settings(tmp_path / "missing", tmp_path, tmp_path))):
            pass


def test_probe_real_report():
    probe_api._cached = None
    with TestClient(probe_api.app) as client:
        assert client.get("/health").json()["runtime_probe"] == "ok"
        report = client.get("/probe").json()
        assert report["status"] == "ok" and report["result"] == 16
        assert len(json.dumps(report).encode()) < 64 * 1024
        assert report["samples"]


def test_probe_failure_is_unhealthy(monkeypatch):
    monkeypatch.setattr(probe_api, "snapshot", lambda: {"status": "error", "result": None})
    with TestClient(probe_api.app) as client:
        assert client.get("/health").status_code == 503


def test_project_api_dispatches_real_builtin_agents(service):
    client, _ = service
    agents = client.get("/v1/agents").json()["agents"]
    assert {"data_processing", "algorithm_execution"} <= {cap for agent in agents for cap in agent["capabilities"]}
    response = client.post("/v1/projects", json={"name": "demo", "context": {"source": "test"}, "tasks": [
        {"id": "stats", "name": "statistics", "capability": "algorithm_execution", "depends_on": ["data"]},
        {"id": "data", "name": "prepare", "capability": "data_processing", "input": {"values": [3, 1, 3, 2]}},
    ]})
    assert response.status_code == 201
    project = response.json()
    assert all(task["status"] == "PENDING" for task in project["tasks"])
    url = f"/v1/projects/{project['id']}/dispatch"
    response = client.post(url)
    assert response.status_code == 200
    result = response.json()
    assert all(task["status"] == "COMPLETED" for task in result["tasks"])
    assert result["tasks"][0]["output"]["mean"] == 2
    assert client.post(url).json() == result


@pytest.mark.parametrize("tasks", [
    [{"id": "a", "name": "a", "capability": "x", "depends_on": ["missing"]}],
    [{"id": "a", "name": "a", "capability": "x", "depends_on": ["a"]}],
    [{"id": "a", "name": "a", "capability": "x"}, {"id": "a", "name": "b", "capability": "x"}],
])
def test_project_api_rejects_invalid_graph_atomically(service, tasks):
    client, _ = service
    assert client.post("/v1/projects", json={"name": "bad", "tasks": tasks}).status_code == 422
    assert client.app.state.projects == {}


def test_project_api_missing_capability_and_conflict(service):
    from rd_platform.domain import TaskStatus

    client, _ = service
    assert client.post("/v1/projects/missing/dispatch").status_code == 404
    project = client.post("/v1/projects", json={"name": "blocked", "tasks": [
        {"id": "a", "name": "a", "capability": "unregistered"},
    ]}).json()
    url = f"/v1/projects/{project['id']}/dispatch"
    result = client.post(url).json()
    assert result["tasks"][0]["status"] == "BLOCKED"
    assert "unregistered" in result["tasks"][0]["error"]
    client.app.state.projects[project["id"]].tasks[0].status = TaskStatus.RUNNING
    assert client.post(url).status_code == 409


def test_project_api_cannot_inject_execution_state(service):
    client, _ = service
    response = client.post("/v1/projects", json={"name": "bad", "tasks": [
        {"name": "a", "capability": "data_processing", "status": "COMPLETED", "output": {}},
    ]})
    assert response.status_code == 422


@pytest.mark.parametrize("status", ["COMPLETED", "FAILED", "BLOCKED"])
def test_protocol_agent_dynamic_catalog_and_api(tmp_path, status):
    from rd_platform.agents import AgentInput, AgentOutput, BaseAgent, CODER
    from rd_platform.domain import AgentMetadata
    from rd_platform.manager import AGENT_REGISTRY, register_agent

    @register_agent("api-echo")
    class EchoAgent(BaseAgent):
        def __init__(self):
            super().__init__(AgentMetadata("api-echo", "API Echo", {"echo"}, role=CODER))

        async def run(self, request: AgentInput) -> AgentOutput:
            return AgentOutput(status=status, artifacts={"received": request.payload}, logs="echo evidence",
                               error=None if status == "COMPLETED" else "review required")

    try:
        settings = Settings(*(tmp_path / name for name in ("data", "logs", "models")))
        for path in (settings.data, settings.logs, settings.models):
            path.mkdir()
        with TestClient(create_app(settings)) as client:
            agents = client.get("/v1/agents").json()["agents"]
            assert next(agent for agent in agents if agent["id"] == "api-echo")["role"] == "CODER"
            payload = {"requirements": ["build a service"], "files": {"app.py": "# candidate"}}
            created = client.post("/v1/projects", json={"name": "generic", "tasks": [
                {"name": "echo", "capability": "echo", "input": payload},
            ]})
            assert created.status_code == 201
            response = client.post(f"/v1/projects/{created.json()['id']}/dispatch")
            assert response.status_code == 200
            task = response.json()["tasks"][0]
            assert task["status"] == status
            assert task["output"] == {"received": payload}
            assert task["logs"] == "echo evidence"
            assert task["error"] == (None if status == "COMPLETED" else "review required")
    finally:
        AGENT_REGISTRY.pop("api-echo")


@pytest.mark.parametrize("configured", [False, True])
def test_architect_skeleton_via_api(tmp_path, configured):
    from rd_platform.agents.dev import ArchitectAgent
    from rd_platform.manager import AgentRegistry

    class FakeGenerator:
        async def generate(self, request):
            return {"architecture_spec": {"modules": [{"name": "app", "responsibility": "handle requests"}],
                                          "data_flow": ["request -> response"]},
                    "api_contracts": {"request": {"type": "object"}},
                    "subtasks": [{"id": "build", "description": "implement app", "acceptance_criteria": ["returns response"]}]}

    registry = AgentRegistry()
    registry.register(ArchitectAgent(FakeGenerator() if configured else None))
    settings = Settings(*(tmp_path / name for name in ("data", "logs", "models")))
    for directory in (settings.data, settings.logs, settings.models):
        directory.mkdir()
    with TestClient(create_app(settings, registry=registry)) as client:
        assert client.get("/v1/agents").json()["agents"][0]["role"] == "ARCHITECT"
        created = client.post("/v1/projects", json={"name": "design", "tasks": [
            {"name": "design", "capability": "architecture", "input": {"requirements": "build a service"}},
        ]})
        assert created.status_code == 201
        result = client.post(f"/v1/projects/{created.json()['id']}/dispatch")
        assert result.status_code == 200
        task = result.json()["tasks"][0]
        assert task["status"] == ("COMPLETED" if configured else "BLOCKED")
        if configured:
            assert task["output"]["subtasks"][0]["id"] == "build"
        else:
            assert task["output"] == {} and "not configured" in task["error"]
