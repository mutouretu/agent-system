"""Agent management page route and existing Agent config API contract tests."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
from fastapi.testclient import TestClient
from rd_platform.service.api import Settings, create_app


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


def make_project(client, name="agents page project"):
    response = client.post("/v1/projects", json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_agents_page_served_with_csp_and_assets(client):
    page = client.get("/agents")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    csp = page.headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "/assets/agents_page.js" in page.text
    assert "/assets/agents_page.css" in page.text
    assert client.get("/assets/agents_page.js").status_code == 200
    assert client.get("/assets/agents_page.css").status_code == 200


def test_agents_page_references_only_dedicated_scripts(client):
    text = client.get("/agents").text
    assert "app.js" not in text
    assert "studio.js" not in text
    assert "agent_config.js" not in text
    assert "agents_page.js" in text


def test_console_navigation_links_agents_outside_subnav(client):
    home = client.get("/")
    assert home.status_code == 200
    text = home.text
    assert 'id="nav-agents"' in text
    assert 'href="/agents"' in text
    # Existing project list and development markers are preserved.
    assert 'id="nav-projects"' in text
    assert 'id="nav-workspace"' in text
    assert 'id="project-subnav"' in text
    subnav_start = text.index('id="project-subnav"')
    subnav_end = text.index('</div>', subnav_start)
    agents_pos = text.index('id="nav-agents"')
    assert agents_pos > subnav_end


def test_get_agent_config_returns_four_roles(client):
    project_id = make_project(client)
    response = client.get(f"/v1/projects/{project_id}/agents/config")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(["ARCHITECT", "CODER", "REVIEWER", "TESTER"]).issubset(body["agents"].keys())
    assert isinstance(body["revision"], int)
    for role in ("ARCHITECT", "CODER", "REVIEWER", "TESTER"):
        spec = body["agents"][role]
        assert {"backend", "model", "responsibility"}.issubset(spec.keys())
    assert body["agents"]["TESTER"]["backend"] == "sandbox"
    assert body["agents"]["TESTER"]["model"] is None


def test_put_agent_config_round_trip_and_conflict(client):
    project_id = make_project(client)
    snapshot = client.get(f"/v1/projects/{project_id}/agents/config").json()
    agents = {role: dict(spec) for role, spec in snapshot["agents"].items()}
    agents["ARCHITECT"]["responsibility"] = "负责架构与任务拆解（更新）"
    saved = client.put(f"/v1/projects/{project_id}/agents/config",
                       json={"revision": snapshot["revision"], "agents": agents})
    assert saved.status_code == 200, saved.text
    first = saved.json()
    reread = client.get(f"/v1/projects/{project_id}/agents/config").json()
    assert reread["agents"]["ARCHITECT"]["responsibility"] == agents["ARCHITECT"]["responsibility"]
    assert reread["revision"] == first["revision"]

    stale = client.put(f"/v1/projects/{project_id}/agents/config",
                       json={"revision": snapshot["revision"], "agents": agents})
    assert stale.status_code == 409
    after = client.get(f"/v1/projects/{project_id}/agents/config").json()
    assert after["revision"] == first["revision"]
    assert after["agents"]["ARCHITECT"]["responsibility"] == agents["ARCHITECT"]["responsibility"]


def test_unknown_project_returns_404(client):
    assert client.get("/v1/projects/missing/agents/config").status_code == 404
    snapshot = {"revision": 0, "agents": {}}
    response = client.put("/v1/projects/missing/agents/config", json=snapshot)
    # Unknown projects surface as 404, or as 422 when validation fires before
    # the store is consulted; both are non-mutating rejection paths.
    assert response.status_code in (404, 422)


def test_invalid_revision_rejected(client):
    project_id = make_project(client)
    snapshot = client.get(f"/v1/projects/{project_id}/agents/config").json()
    for revision in (-1, "0", True, 1.5):
        response = client.put(f"/v1/projects/{project_id}/agents/config",
                              json={"revision": revision, "agents": snapshot["agents"]})
        assert response.status_code == 422, (revision, response.text)


def test_tester_must_use_sandbox(client):
    project_id = make_project(client)
    snapshot = client.get(f"/v1/projects/{project_id}/agents/config").json()
    agents = {role: dict(spec) for role, spec in snapshot["agents"].items()}
    agents["TESTER"]["backend"] = "codex"
    response = client.put(f"/v1/projects/{project_id}/agents/config",
                          json={"revision": snapshot["revision"], "agents": agents})
    assert response.status_code == 422, response.text


def test_cross_origin_put_forbidden(client):
    project_id = make_project(client)
    snapshot = client.get(f"/v1/projects/{project_id}/agents/config").json()
    response = client.put(f"/v1/projects/{project_id}/agents/config",
                          json={"revision": snapshot["revision"], "agents": snapshot["agents"]},
                          headers={"origin": "http://evil.example"})
    assert response.status_code == 403
