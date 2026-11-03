"""Conversation identity and human acceptance are durable work item boundaries."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
from fastapi.testclient import TestClient

from rd_platform.service.api import Settings, create_app


@pytest.fixture
def client(tmp_path):
    settings = Settings(*(tmp_path / name for name in ("data", "logs", "models")))
    for directory in (settings.data, settings.logs, settings.models):
        directory.mkdir()
    with TestClient(create_app(settings)) as browser:
        yield browser


def finish_pass(client, run_id):
    worker_id = "a" * 32
    headers = {"X-Worker-Token": client.app.state.worker_token}
    client.post("/internal/worker/heartbeat", headers=headers,
                json={"worker_id": worker_id, "backends": ["codex"]}).raise_for_status()
    claimed = client.post("/internal/worker/claim", headers=headers,
                          json={"worker_id": worker_id}).json()["run"]
    assert claimed["id"] == run_id
    response = client.post(f"/internal/worker/runs/{run_id}/finish", headers=headers,
        json={"worker_id": worker_id, "generation": claimed["generation"],
              "summary": {"status": "PASS", "execution_mode": "docker",
                          "final_test_summary": {"outcome": "PASSED", "total": 2,
                                                 "counts": {"passed": 2}}}})
    assert response.status_code == 200, response.text


def test_work_item_remains_open_until_human_accepts_latest_run(client):
    project = client.post("/v1/projects", json={"name": "R&D"}).json()
    first = client.post("/v1/runs", json={"project_id": project["id"],
        "requirements": "Implement safe cache", "backend": "codex"}).json()
    item_id = first["id"]
    item = client.get(f"/v1/work-items/{item_id}").json()
    assert item["status"] == "IN_PROGRESS"
    assert client.post(f"/v1/work-items/{item_id}/accept", json={"revision": 0,
        "run_id": item_id}).status_code == 409

    finish_pass(client, item_id)
    item = client.get(f"/v1/work-items/{item_id}").json()
    assert item["status"] == "AWAITING_ACCEPTANCE"
    assert client.post(f"/v1/work-items/{item_id}/accept", json={"revision": 0,
        "run_id": "b" * 32}).status_code == 409
    accepted = client.post(f"/v1/work-items/{item_id}/accept", json={"revision": 0,
        "run_id": item_id})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["status"] == "DELIVERED"
    assert accepted.json()["accepted_run_id"] == item_id
    assert client.post(f"/v1/work-items/{item_id}/accept", json={"revision": 0,
        "run_id": item_id}).status_code == 409

    next_run = client.post(f"/v1/work-items/{item_id}/runs",
        json={"instruction": "Add an expiry boundary test"})
    assert next_run.status_code == 202, next_run.text
    child_id = next_run.json()["run"]["id"]
    assert client.post(f"/v1/work-items/{item_id}/runs",
        json={"instruction": "A second concurrent revision"}).status_code == 409
    item = client.get(f"/v1/work-items/{item_id}").json()
    assert item["id"] == item_id
    assert item["run_ids"] == [item_id, child_id]
    assert item["status"] == "IN_PROGRESS"
    assert client.get(f"/v1/work-items/{child_id}").status_code == 404
    finish_pass(client, child_id)
    item = client.get(f"/v1/work-items/{item_id}").json()
    assert item["status"] == "AWAITING_ACCEPTANCE"
    assert item["accepted_run_id"] == item_id
    assert client.post(f"/v1/work-items/{item_id}/accept", json={"revision": 1,
        "run_id": child_id}).json()["status"] == "DELIVERED"


def test_discussion_is_in_same_work_item_timeline_without_starting_development(client):
    project = client.post("/v1/projects", json={"name": "R&D"}).json()
    run = client.post("/v1/runs", json={"project_id": project["id"],
        "requirements": "Build a parser", "backend": "codex"}).json()
    item_id = run["id"]
    finish_pass(client, item_id)
    reply = client.post(f"/v1/runs/{item_id}/chat", json={"message": "为什么测试失败？"})
    assert reply.status_code == 202
    headers = {"X-Worker-Token": client.app.state.worker_token}
    chat_job = client.post("/internal/worker/claim", headers=headers,
        json={"worker_id": "a" * 32}).json()["run"]
    assert chat_job["template"] == "chat"
    assert client.post(f"/internal/worker/runs/{chat_job['id']}/finish", headers=headers,
        json={"worker_id": "a" * 32, "generation": chat_job["generation"],
              "summary": {"status": "CHAT_READY", "answer": "测试已通过，等待业务验收。"}}).status_code == 200
    item = client.get(f"/v1/work-items/{item_id}").json()
    assert item["run_ids"] == [item_id]
    assert item["status"] == "AWAITING_ACCEPTANCE"
    assert any(message["speaker"] == "你" and message["text"] == "为什么测试失败？"
               for message in item["timeline"])
    assert any(message["speaker"] == "Team Lead" and message["text"] == "测试已通过，等待业务验收。"
               for message in item["timeline"])
    assert client.get("/v1/work-items").json()["work_items"][0]["id"] == item_id


def test_work_item_is_linked_to_submitted_requirement(client):
    project = client.post("/v1/projects", json={"name": "R&D"}).json()
    req = client.post(f"/v1/projects/{project['id']}/requirements").json()
    document = {"title": "Cache", "business_context": "Cache records safely",
                "feature_packages": [{"id": "PKG-1", "name": "Storage", "description": "Store records",
                                      "priority": "P0", "group": "Core"}],
                "acceptance_criteria": ["Reads return stored values"]}
    response = client.post(f"/v1/projects/{project['id']}/requirements/{req['id']}/submit",
        json={"revision": 0, "document": document, "backend": "codex"})
    assert response.status_code == 202, response.text
    item_id = response.json()["run_id"]
    item = client.get(f"/v1/work-items/{item_id}").json()
    assert item["requirement_id"] == req["id"]
    assert item["title"] == "Cache"
    assert item["acceptance_criteria"] == ["Reads return stored values"]
    finish_pass(client, item_id)
    assert client.post(f"/v1/work-items/{item_id}/accept", json={"revision": 0,
        "run_id": item_id}).status_code == 422
    accepted = client.post(f"/v1/work-items/{item_id}/accept", json={"revision": 0,
        "run_id": item_id, "criteria_confirmed": ["Reads return stored values"]})
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "DELIVERED"


def test_create_change_work_item_under_existing_requirement(client):
    project = client.post("/v1/projects", json={"name": "R&D"}).json()
    req = client.post(f"/v1/projects/{project['id']}/requirements").json()
    payload = {"project_id": project["id"], "requirement_id": req["id"],
               "kind": "CHANGE", "title": "Remove duplicate card",
               "description": "Keep Agent configuration on its own page",
               "acceptance_criteria": ["Project page has no duplicate card"],
               "backend": "codex", "workspace_mode": "repository"}
    created = client.post("/v1/work-items", json=payload)
    assert created.status_code == 202, created.text
    item_id = created.json()["work_item_id"]
    item = client.get(f"/v1/work-items/{item_id}").json()
    assert item["kind"] == "CHANGE"
    assert item["title"] == payload["title"]
    assert item["description"] == payload["description"]
    assert item["requirement_id"] == req["id"]
    assert item["acceptance_criteria"] == payload["acceptance_criteria"]
    assert len(client.get("/v1/work-items", params={"requirement_id": req["id"]}).json()["work_items"]) == 1
    other = client.post("/v1/projects", json={"name": "other"}).json()
    assert client.post("/v1/work-items", json={**payload, "project_id": other["id"]}).status_code == 404
