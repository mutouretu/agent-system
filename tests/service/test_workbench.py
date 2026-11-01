"""Workbench API tests; all pytest execution stays in Docker."""

import json
import time

import pytest
pytest.importorskip("fastapi")
pytest.importorskip("sqlalchemy")
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from rd_platform.service.api import Settings, create_app
from rd_platform.service.run_store import RunStore, StoreConflict, runs


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


def submit(client, **options):
    project = client.post("/v1/projects", json={"name": "workbench test"}).json()
    response = client.post("/v1/runs", json={"project_id": project["id"], "requirements": "implement cache", **options})
    assert response.status_code == 202, response.text
    return response.json()


def worker(client, worker_id="a" * 32):
    headers = {"X-Worker-Token": client.app.state.worker_token}
    assert client.post("/internal/worker/heartbeat", json={"worker_id": worker_id, "backends": ["codex"]}, headers=headers).status_code == 200
    return headers, worker_id


def test_project_run_persistence_across_restart(settings):
    with TestClient(create_app(settings)) as client:
        run = submit(client)
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/projects").json()["projects"][0]["id"] == run["project_id"]
        assert client.get(f"/v1/runs/{run['id']}").json()["status"] == "QUEUED"
        assert client.get(f"/v1/projects/{run['project_id']}/").status_code == 200


def test_project_details_keep_context_and_scope_run_history(client):
    project = client.post("/v1/projects", json={
        "name": "Project details", "context": {"description": "Build a cache"},
        "tasks": [{"id": "prepare", "name": "Prepare data", "capability": "data_processing", "input": {}}],
    }).json()
    assert client.get("/v1/runs", params={"project_id": project["id"]}).json()["runs"] == []
    own = []
    for _ in range(2):
        response = client.post("/v1/runs", json={"project_id": project["id"], "requirements": "cache"})
        assert response.status_code == 202
        own.append(response.json()["id"])
    other = submit(client)
    history = client.get("/v1/runs", params={"project_id": project["id"]}).json()["runs"]
    assert [run["id"] for run in history] == list(reversed(own))
    assert other["id"] not in {run["id"] for run in history}
    detail = client.get(f"/v1/projects/{project['id']}").json()
    assert detail["context"] == {"description": "Build a cache"}
    assert detail["tasks"][0]["id"] == "prepare"
    assert detail["tasks"][0]["status"] == "PENDING"


def test_async_submission_claim_events_artifacts_and_finish(client):
    run = submit(client)
    headers, worker_id = worker(client)
    claim = client.post("/internal/worker/claim", json={"worker_id": worker_id}, headers=headers).json()["run"]
    assert claim["id"] == run["id"] and claim["generation"] == 1
    lease = {"worker_id": worker_id, "generation": 1}
    task = {"id": "coder-00", "name": "Implementation", "capability": "coding", "status": "COMPLETED",
            "output": {"files": {"src/main.py": "<script>alert('data only')</script>"}, "tests": {}}}
    url = f"/internal/worker/runs/{run['id']}"
    assert client.post(url + "/events", json={**lease, "kind": "task_finished", "task": task}, headers=headers).status_code == 200
    assert client.post(url + "/events", json={**lease, "kind": "task_finished", "task": task}, headers=headers).status_code == 200
    artifacts = client.get(f"/v1/runs/{run['id']}/artifacts").json()["artifacts"]
    assert artifacts[0]["content"].startswith("<script>")  # Returned as JSON data, never executed.
    assert client.post(url + "/finish", json={**lease, "summary": {"status": "FAILED", "error": "intentional test"}}, headers=headers).status_code == 200
    response = client.get(f"/v1/runs/{run['id']}/events")
    assert "text/event-stream" in response.headers["content-type"]
    assert "task_finished" in response.text and "event: done" in response.text
    assert response.text.count('"kind": "task_finished"') == 1
    last_id = max(int(line[4:]) for line in response.text.splitlines() if line.startswith("id: "))
    replay = client.get(f"/v1/runs/{run['id']}/events", headers={"Last-Event-ID": str(last_id)})
    assert "task_finished" not in replay.text and "event: done" in replay.text


def test_worker_auth_and_cross_origin_mutation(client):
    assert client.post("/internal/worker/claim", json={"worker_id": "a" * 32}).status_code == 401
    assert client.post("/v1/projects", json={"name": "bad"}, headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/v1/projects", json={"name": "ok"}, headers={"Origin": "http://testserver"}).status_code == 201
    assert "worker_token" not in client.get("/v1/workbench").text


def test_queue_ownership_generation_and_backend_matching(client):
    first, second = submit(client), submit(client, backend="llm")
    headers, owner = worker(client)
    claimed = client.post("/internal/worker/claim", json={"worker_id": owner}, headers=headers).json()["run"]
    assert claimed["id"] == first["id"]
    replay = client.post("/internal/worker/claim", json={"worker_id": owner}, headers=headers).json()["run"]
    assert replay["id"] == first["id"] and replay["generation"] == 1
    _, other = worker(client, "b" * 32)
    assert client.post("/internal/worker/claim", json={"worker_id": other}, headers=headers).json()["run"] is None
    for worker_id, generation in ((other, 1), (owner, 2)):
        response = client.post(f"/internal/worker/runs/{first['id']}/finish", headers=headers,
            json={"worker_id": worker_id, "generation": generation, "summary": {"status": "FAILED"}})
        assert response.status_code == 409
    assert client.get(f"/v1/runs/{second['id']}").json()["status"] == "QUEUED"


def test_expired_lease_is_interrupted_and_cannot_publish(client):
    run = submit(client)
    headers, worker_id = worker(client)
    client.post("/internal/worker/claim", json={"worker_id": worker_id}, headers=headers)
    database = client.app.state.run_store
    with database.transaction() as db:
        db.execute(runs.update().where(runs.c.id == run["id"]).values(lease_until=time.time()-1))
    database.heartbeat(worker_id, ["codex"])
    assert client.get(f"/v1/runs/{run['id']}").json()["status"] == "INTERRUPTED"
    assert client.post(f"/internal/worker/runs/{run['id']}/finish", headers=headers,
        json={"worker_id": worker_id, "generation": 1, "summary": {"status": "FAILED"}}).status_code == 409


def test_fake_success_is_not_accepted_as_real_test_result(client):
    run = submit(client)
    headers, worker_id = worker(client)
    client.post("/internal/worker/claim", json={"worker_id": worker_id}, headers=headers)
    assert client.post(f"/internal/worker/runs/{run['id']}/finish", headers=headers,
        json={"worker_id": worker_id, "generation": 1, "summary": {"status": "PASS", "execution_mode": "fake"}}).status_code == 422


@pytest.mark.parametrize("options", [{"backend": "shell"}, {"max_retries": 4}, {"requirements": " "}, {"template": "custom-command"}])
def test_run_input_validation(client, options):
    project = client.post("/v1/projects", json={"name": "validation"}).json()
    assert client.post("/v1/runs", json={"project_id": project["id"], "requirements": "cache", **options}).status_code == 422


def test_static_ui_and_missing_entities(client):
    home = client.get("/")
    assert home.status_code == 200 and "研发管理控制台" in home.text
    assert "script-src 'self'" in home.headers["content-security-policy"]
    script = client.get("/assets/app.js")
    assert script.status_code == 200 and "EventSource" in script.text
    assert "innerHTML" not in script.text and "eval(" not in script.text
    assert client.get("/v1/runs/missing").status_code == 404
    assert client.get("/v1/projects/missing").status_code == 404
    assert client.post("/v1/runs", json={"project_id": "f" * 32, "requirements": "cache"}).status_code == 404


def test_postgres_claim_uses_skip_locked():
    sql = str(select(runs).where(runs.c.status == "QUEUED").with_for_update(skip_locked=True).compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql


def test_workspace_persistence_revision_and_run_scope(client):
    project = client.post('/v1/projects', json={'name': 'Workspace', 'context': {'requirements': 'Keep me'}}).json()
    url = f"/v1/projects/{project['id']}/workspace"
    payload = {'revision': 0, 'milestones': [{'name': 'M1', 'goal': 'Core', 'packages': ['Cache']}], 'accepted': []}
    response = client.put(url, json=payload)
    assert response.status_code == 200
    assert response.json()['context']['requirements'] == 'Keep me'
    saved = client.get(f"/v1/projects/{project['id']}").json()['context']['workspace']
    assert saved['revision'] == 1
    assert saved['milestones'][0]['packages'] == ['Cache']
    assert client.put(url, json=payload).status_code == 409
    other_run = submit(client)
    payload['revision'] = 1
    payload['milestones'][0]['run_id'] = other_run['id']
    assert client.put(url, json=payload).status_code == 409
    payload['milestones'][0]['run_id'] = None
    payload['accepted'] = ['acceptance-for-this-run']
    assert client.put(url, json=payload).json()['context']['workspace']['accepted'] == payload['accepted']
    payload['milestones'][0]['name'] = ''
    assert client.put(url, json=payload).status_code == 422
    assert client.put('/v1/projects/' + '0' * 32 + '/workspace', json={'revision': 0, 'milestones': []}).status_code == 404
