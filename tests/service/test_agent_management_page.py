"""Standalone Agent management page and agent-config API contract tests."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
from fastapi.testclient import TestClient

from rd_platform.service.api import Settings, create_app
from rd_platform.service.page_routes import CSP_POLICY


ROLE_KEYS = ("ARCHITECT", "CODER", "REVIEWER", "TESTER")
PUBLIC_ROLE_KEYS = {"backend", "model", "responsibility"}


@pytest.fixture
def settings(tmp_path):
    result = Settings(*(tmp_path / name for name in ("data", "logs", "models")))
    for path in (result.data, result.logs, result.models):
        path.mkdir()
    return result


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as client:
        yield client


def make_project(client, name="Agent page project"):
    response = client.post("/v1/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()


def config_path(project_id):
    return f"/v1/projects/{project_id}/agents/config"


def test_agents_page_is_served_with_security_headers(client):
    response = client.get("/agents")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert response.headers.get("x-content-type-options") == "nosniff"
    assert response.headers.get("content-security-policy") == CSP_POLICY
    body = response.text
    assert '<script src="/assets/agents.js"></script>' in body
    assert '/assets/agents.css' in body
    assert 'id="project-select"' in body
    assert 'id="roles"' in body
    assert 'aria-current="page"' in body
    assert 'href="/#development"' in body


def test_agents_static_assets_are_served(client):
    script = client.get("/assets/agents.js")
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    assert "/agents/config" in script.text

    style = client.get("/assets/agents.css")
    assert style.status_code == 200
    assert "text/css" in style.headers["content-type"]


def test_agents_page_has_no_secret_controls_or_storage(client):
    body = client.get("/agents").text.lower()
    assert "api_key" not in body
    assert "apikey" not in body

    script = client.get("/assets/agents.js").text.lower()
    assert "api_key" not in script
    for storage in ("localstorage", "sessionstorage", "indexeddb"):
        assert storage not in script


def test_agent_config_initial_shape(client):
    project = make_project(client)
    response = client.get(config_path(project["id"]))
    assert response.status_code == 200
    document = response.json()
    assert document["revision"] == 0
    assert set(document["agents"]) == set(ROLE_KEYS)
    for role in ROLE_KEYS:
        config = document["agents"][role]
        assert PUBLIC_ROLE_KEYS <= set(config)
        assert isinstance(config["backend"], str)
        assert isinstance(config["responsibility"], str)
    assert document["agents"]["TESTER"]["backend"] == "sandbox"


def test_agent_config_put_increments_revision_and_conflicts(client):
    project = make_project(client)
    path = config_path(project["id"])
    agents = client.get(path).json()["agents"]
    agents["CODER"]["model"] = "page-model"

    saved = client.put(path, json={"revision": 0, "agents": agents})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert saved.json()["agents"]["CODER"]["model"] == "page-model"

    stale = client.put(path, json={"revision": 0, "agents": agents})
    assert stale.status_code == 409
    assert client.get(path).json()["agents"]["CODER"]["model"] == "page-model"


def test_agent_config_rejects_secrets_and_non_sandbox_tester_without_persisting(client):
    project = make_project(client)
    path = config_path(project["id"])
    agents = client.get(path).json()["agents"]

    agents["CODER"]["api_key"] = "do-not-store"
    response = client.put(path, json={"revision": 0, "agents": agents})
    assert response.status_code == 422
    assert "do-not-store" not in response.text
    agents["CODER"].pop("api_key")

    agents["TESTER"] = {"backend": "llm", "model": "m", "responsibility": "test"}
    assert client.put(path, json={"revision": 0, "agents": agents}).status_code == 422

    current = client.get(path).json()
    assert current["revision"] == 0
    assert current["agents"]["TESTER"]["backend"] == "sandbox"


def test_agent_config_project_isolation_and_unknown_project(client):
    first = make_project(client, "First")
    second = make_project(client, "Second")

    agents = client.get(config_path(first["id"])).json()["agents"]
    agents["REVIEWER"]["model"] = "only-first"
    assert client.put(config_path(first["id"]), json={"revision": 0, "agents": agents}).status_code == 200

    second_doc = client.get(config_path(second["id"])).json()
    assert second_doc["revision"] == 0
    assert second_doc["agents"]["REVIEWER"]["model"] != "only-first"

    assert client.get(config_path("missing-project")).status_code == 404
    assert client.put(config_path("missing-project"), json={"revision": 0, "agents": agents}).status_code == 404


def test_agent_config_run_snapshot_is_preserved(client):
    project = make_project(client)
    path = config_path(project["id"])
    agents = client.get(path).json()["agents"]
    agents["CODER"]["model"] = "snapshot-model"
    saved = client.put(path, json={"revision": 0, "agents": agents})
    assert saved.status_code == 200

    run = client.post("/v1/runs", json={"project_id": project["id"],
                                        "requirements": "cache", "backend": "configured"})
    assert run.status_code == 202, run.text
    snapshot = run.json()["summary"]["agent_config"]
    assert snapshot["agents"]["CODER"]["model"] == "snapshot-model"

    agents["CODER"]["model"] = "later-model"
    assert client.put(path, json={"revision": 1, "agents": agents}).status_code == 200
    detail = client.get(f"/v1/runs/{run.json()['id']}").json()
    assert detail["summary"]["agent_config"]["agents"]["CODER"]["model"] == "snapshot-model"


def test_agent_config_rejects_non_integer_revision(client):
    project = make_project(client)
    agents = client.get(config_path(project["id"])).json()["agents"]
    assert client.put(config_path(project["id"]), json={"revision": True, "agents": agents}).status_code == 422
    assert client.put(config_path(project["id"]), json={"revision": "0", "agents": agents}).status_code == 422


def test_root_page_links_to_agent_management(client):
    response = client.get("/")
    assert response.status_code == 200
    assert 'href="/agents"' in response.text
    assert "Agent 管理" in response.text
