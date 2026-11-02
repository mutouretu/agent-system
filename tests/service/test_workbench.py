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


def test_chat_action_is_typed_and_never_inferred_from_message(client):
    parent = submit(client)
    headers, worker_id = worker(client)
    claimed = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    assert client.post(f"/internal/worker/runs/{parent['id']}/finish", headers=headers,
        json={'worker_id': worker_id, 'generation': claimed['generation'],
              'summary': {'status': 'FAILED', 'error': 'test failure'}}).status_code == 200
    chat = client.post(f"/v1/runs/{parent['id']}/chat",
        json={'message': '请继续刚才中断的流程'})
    assert chat.status_code == 202
    job = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    assert 'action' not in job['summary']
    assert client.post(f"/v1/runs/{parent['id']}/chat",
        json={'message': '继续', 'action': 'unknown'}).status_code == 422


def test_worker_resume_chat_enqueues_without_calling_model(tmp_path):
    from scripts.workbench_worker import Worker

    class Response:
        status_code = 202

        def json(self):
            return {'id': 'b' * 32, 'summary': {'resume_from_task': 'reviewer-00'}}

    posted = []
    worker_instance = Worker.__new__(Worker)
    worker_instance.directory = tmp_path
    worker_instance.client = type('Client', (), {'post': lambda self, path, json: (posted.append((path, json)), Response())[1]})()
    worker_instance.post = lambda path, **data: posted.append((path, data))
    job = {'id': 'c' * 32, 'generation': 1,
           'summary': {'parent_run_id': 'a' * 32, 'action': {'type': 'resume',
                       'parent_run_id': 'a' * 32, 'task_id': None}}}
    worker_instance.run_chat(job)
    assert posted[0] == (f"/v1/runs/{'a' * 32}/resume", {'task_id': None})
    assert posted[1][0].endswith('/finish')
    assert posted[1][1]['summary']['resume_run_id'] == 'b' * 32

    posted.clear()
    job['summary']['action'] = {'type': 'report_existing', 'run_id': 'b' * 32,
        'status': 'COMPLETED', 'acceptance': 'generated_tests_only',
        'final_test_summary': {'total': 14, 'counts': {'passed': 14}}}
    worker_instance.run_chat(job)
    assert len(posted) == 1 and posted[0][0].endswith('/finish')
    assert '14/14' in posted[0][1]['summary']['answer']
    assert '尚不代表产物已部署' in posted[0][1]['summary']['answer']


def test_project_run_persistence_across_restart(settings):
    with TestClient(create_app(settings)) as client:
        run = submit(client)
    with TestClient(create_app(settings)) as client:
        assert client.get("/v1/projects").json()["projects"][0]["id"] == run["project_id"]
        assert client.get(f"/v1/runs/{run['id']}").json()["status"] == "QUEUED"
        assert client.get(f"/v1/projects/{run['project_id']}/").status_code == 200


def test_repository_run_requires_git_delivery_before_completion(client):
    run = submit(client, workspace_mode='repository')
    assert run['summary']['workspace_mode'] == 'repository'
    headers, worker_id = worker(client)
    claimed = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    path = f"/internal/worker/runs/{run['id']}/finish"
    base = {'status': 'PASS', 'execution_mode': 'docker',
            'final_test_summary': {'outcome': 'PASSED', 'total': 1,
                                   'counts': {'passed': 1, 'failed': 0, 'errors': 0, 'skipped': 0}}}
    assert client.post(path, headers=headers,
        json={'worker_id': worker_id, 'generation': claimed['generation'], 'summary': base}).status_code == 422
    delivered = {**base, 'delivery': {'status': 'COMMITTED', 'run_id': run['id'], 'commit': 'a' * 40}}
    assert client.post(path, headers=headers,
        json={'worker_id': worker_id, 'generation': claimed['generation'], 'summary': delivered}).status_code == 200
    result = client.get(f"/v1/runs/{run['id']}").json()
    assert result['status'] == 'COMPLETED' and result['summary']['delivery']['commit'] == 'a' * 40


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


def test_agent_config_persists_and_configured_run_snapshots_roles(client):
    project = client.post("/v1/projects", json={"name": "Agent project"}).json()
    path = f"/v1/projects/{project['id']}/agents/config"
    initial = client.get(path).json()
    assert initial["revision"] == 0
    assert initial["agents"]["TESTER"]["backend"] == "sandbox"
    assert client.post("/v1/runs", json={"project_id": project["id"],
        "requirements": "implement a function", "backend": "configured"}).status_code == 409
    agents = initial["agents"]
    agents["CODER"]["model"] = "deepseek-v4.1-flash"
    saved = client.put(path, json={"revision": 0, "agents": agents})
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1
    assert client.put(path, json={"revision": 0, "agents": agents}).status_code == 409
    assert client.get(path).json() == saved.json()
    run = client.post("/v1/runs", json={"project_id": project["id"],
        "requirements": "implement a function", "backend": "configured"})
    assert run.status_code == 202, run.text
    assert run.json()["summary"]["agent_config"] == saved.json()
    agents["CODER"]["model"] = "another-model"
    assert client.put(path, json={"revision": 1, "agents": agents}).status_code == 200
    assert client.get(f"/v1/runs/{run.json()['id']}").json()["summary"]["agent_config"]["agents"]["CODER"]["model"] == "deepseek-v4.1-flash"


def test_agent_config_rejects_secrets_and_non_sandbox_tester(client):
    project = client.post("/v1/projects", json={"name": "Agent project"}).json()
    path = f"/v1/projects/{project['id']}/agents/config"
    agents = client.get(path).json()["agents"]
    agents["CODER"]["api_key"] = "do-not-store"
    assert client.put(path, json={"revision": 0, "agents": agents}).status_code == 422
    agents["CODER"].pop("api_key")
    agents["TESTER"] = {"backend": "llm", "model": "some-model", "responsibility": "test"}
    assert client.put(path, json={"revision": 0, "agents": agents}).status_code == 422
    assert client.get(path).json()["revision"] == 0


def test_run_discussion_keeps_parent_context_and_never_creates_feature_run(client):
    parent = submit(client)
    headers, worker_id = worker(client)
    claimed = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    assert claimed['id'] == parent['id']
    finished = client.post(f"/internal/worker/runs/{parent['id']}/finish", headers=headers,
        json={'worker_id': worker_id, 'generation': claimed['generation'],
              'summary': {'status': 'FAILED', 'error': 'ReviewDecision issues.1.suggestion Field required'}})
    assert finished.status_code == 200, finished.text
    endpoint = f"/v1/runs/{parent['id']}/chat"
    question = client.post(endpoint, json={'message': 'Reviewer 为什么失败？'})
    assert question.status_code == 202, question.text
    assert client.post(endpoint, json={'message': '再问一次'}).status_code == 409
    chat = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    assert chat['id'] == question.json()['id'] and chat['template'] == 'chat'
    assert chat['requirements'] == 'Reviewer 为什么失败？'
    assert 'issues.1.suggestion' in chat['summary']['chat_context']
    ready = client.post(f"/internal/worker/runs/{chat['id']}/finish", headers=headers,
        json={'worker_id': worker_id, 'generation': chat['generation'],
              'summary': {'status': 'CHAT_READY', 'answer': 'Reviewer 的输出缺少 suggestion 字段。'}})
    assert ready.status_code == 200, ready.text
    messages = client.get(endpoint).json()['messages']
    assert messages[0]['message'] == 'Reviewer 为什么失败？'
    assert messages[0]['answer'] == 'Reviewer 的输出缺少 suggestion 字段。'
    followup = client.post(endpoint, json={'message': '那下一步怎样修复？'})
    assert followup.status_code == 202
    second = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    assert second['id'] == followup.json()['id']
    assert 'Reviewer 的输出缺少 suggestion 字段。' in second['summary']['chat_context']
    assert client.post(f"/internal/worker/runs/{second['id']}/finish", headers=headers,
        json={'worker_id': worker_id, 'generation': second['generation'],
              'summary': {'status': 'CHAT_READY', 'answer': '补全建议并重新审查。'}}).status_code == 200
    assert len(client.get(endpoint).json()['messages']) == 2
    assert [run['id'] for run in client.get('/v1/runs', params={'project_id': parent['project_id']}).json()['runs']] == [parent['id']]


@pytest.mark.parametrize('resume_task,prefix_length', [('architect', 0), ('coder-00', 1), ('reviewer-00', 2)])
def test_resume_review_queues_checkpoint_without_mutating_parent(client, resume_task, prefix_length):
    architecture = {'architecture_spec': {'modules': [{'name': 'cache', 'responsibility': 'store'}],
                    'data_flow': ['input to output']}, 'api_contracts': {'cache': {'type': 'object'}},
                    'subtasks': [{'id': 'code', 'description': 'implement', 'acceptance_criteria': ['works']}]}
    code = {'files': {'src/cache.py': 'def get(): return 1\n'},
            'tests': {'tests/test_cache.py': 'def test_get(): assert True\n'},
            'commit_message': 'implement'}

    parent = submit(client)
    headers, worker_id = worker(client)
    claimed = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    path = f"/internal/worker/runs/{parent['id']}"
    for task_id, capability, output, status in [
        ('architect', 'architecture', architecture, 'COMPLETED'),
        ('coder-00', 'coding', code, 'COMPLETED'),
        ('reviewer-00', 'review', None, 'FAILED')]:
        response = client.post(path + '/events', headers=headers,
            json={'worker_id': worker_id, 'generation': claimed['generation'], 'kind': 'task_finished',
                  'task': {'id': task_id, 'capability': capability, 'status': status, 'output': output}})
        assert response.status_code == 200, response.text
    assert client.post(path + '/finish', headers=headers,
        json={'worker_id': worker_id, 'generation': claimed['generation'],
              'summary': {'status': 'FAILED', 'error': 'Reviewer schema failure'}}).status_code == 200
    chat_response = client.post(f"/v1/runs/{parent['id']}/chat",
        json={'message': '继续当前研发运行', 'action': 'resume'})
    assert chat_response.status_code == 202, chat_response.text
    action_job = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    assert action_job['template'] == 'chat'
    assert action_job['summary']['action'] == {'type': 'resume', 'parent_run_id': parent['id'], 'task_id': None}
    assert client.post(f"/internal/worker/runs/{action_job['id']}/finish", headers=headers,
        json={'worker_id': worker_id, 'generation': action_job['generation'],
              'summary': {'status': 'CHAT_READY', 'answer': '恢复入口已准备。'}}).status_code == 200
    assert client.post(f"/v1/runs/{parent['id']}/resume", json={'task_id': 'unknown'}).status_code == 409
    response = client.post(f"/v1/runs/{parent['id']}/resume", json={'task_id': resume_task})
    assert response.status_code == 202, response.text
    child = response.json()
    assert child['template'] == 'resume' and child['status'] == 'QUEUED'
    assert child['summary']['parent_run_id'] == parent['id']
    assert 'resume_seed' not in child['summary']
    listed = client.get('/v1/runs', params={'project_id': parent['project_id']}).json()['runs']
    assert next(item for item in listed if item['id'] == child['id'])['summary']['parent_run_id'] == parent['id']
    assert client.get(f"/v1/runs/{parent['id']}").json()['status'] == 'FAILED'
    assert client.post(f"/v1/runs/{parent['id']}/resume", json={}).status_code == 409
    next_claim = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    assert next_claim['id'] == child['id']
    assert next_claim['summary']['resume_seed']['target_task_id'] == resume_task
    assert len(next_claim['summary']['resume_seed']['tasks']) == prefix_length
    if prefix_length == 2:
        assert next_claim['summary']['resume_seed']['tasks'][1]['output'] == code
    result = {'outcome': 'PASSED', 'total': 14,
              'counts': {'passed': 14, 'failed': 0, 'errors': 0, 'skipped': 0}, 'pass_rate': 100.0}
    child_path = f"/internal/worker/runs/{child['id']}"
    assert client.post(child_path + '/events', headers=headers,
        json={'worker_id': worker_id, 'generation': next_claim['generation'], 'kind': 'task_finished',
              'task': {'id': 'tester-00', 'capability': 'testing', 'status': 'COMPLETED',
                       'output': {'test_summary': result, 'evidence_logs': {'execution_mode': 'docker',
                                  'exit_code': 0, 'run_id': 'sandbox-test', 'logs': '14 passed'}}}}).status_code == 200
    assert client.post(child_path + '/finish', headers=headers,
        json={'worker_id': worker_id, 'generation': next_claim['generation'],
              'summary': {'status': 'PASS', 'execution_mode': 'docker',
                          'final_test_summary': result, 'acceptance': 'generated_tests_only'}}).status_code == 200
    question = client.post(f"/v1/runs/{parent['id']}/chat", json={'message': 'Tester 又失败了吗？'})
    assert question.status_code == 202
    discussion = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    context = json.loads(discussion['summary']['chat_context'])
    latest = context['related_runs_newest_first'][0]
    assert latest['run_id'] == child['id'] and latest['status'] == 'COMPLETED'
    assert latest['final_test_summary']['counts']['passed'] == 14
    assert latest['tasks'][-1]['output']['test_summary']['outcome'] == 'PASSED'
    assert context['run_status'] == 'FAILED'  # Historical status remains distinct.
    assert client.post(f"/internal/worker/runs/{discussion['id']}/finish", headers=headers,
        json={'worker_id': worker_id, 'generation': discussion['generation'],
              'summary': {'status': 'CHAT_READY', 'answer': '14/14 项通过。'}}).status_code == 200
    again = client.post(f"/v1/runs/{parent['id']}/chat",
        json={'message': '继续当前研发运行', 'action': 'resume'})
    assert again.status_code == 202
    existing = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    assert existing['summary']['action']['type'] == 'report_existing'
    assert existing['summary']['action']['run_id'] == child['id']


def test_continue_action_follows_latest_failed_child(client):
    parent = submit(client)
    headers, worker_id = worker(client)
    original = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    task = {'id': 'architect', 'capability': 'architecture', 'status': 'FAILED', 'error': 'temporary failure'}
    assert client.post(f"/internal/worker/runs/{parent['id']}/events", headers=headers,
        json={'worker_id': worker_id, 'generation': original['generation'],
              'kind': 'task_finished', 'task': task}).status_code == 200
    assert client.post(f"/internal/worker/runs/{parent['id']}/finish", headers=headers,
        json={'worker_id': worker_id, 'generation': original['generation'],
              'summary': {'status': 'FAILED', 'error': 'temporary failure'}}).status_code == 200
    child_response = client.post(f"/v1/runs/{parent['id']}/resume", json={'task_id': 'architect'})
    assert child_response.status_code == 202
    child = child_response.json()
    claimed = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    assert claimed['id'] == child['id']
    assert client.post(f"/internal/worker/runs/{child['id']}/events", headers=headers,
        json={'worker_id': worker_id, 'generation': claimed['generation'],
              'kind': 'task_finished', 'task': task}).status_code == 200
    assert client.post(f"/internal/worker/runs/{child['id']}/finish", headers=headers,
        json={'worker_id': worker_id, 'generation': claimed['generation'],
              'summary': {'status': 'FAILED', 'error': 'still failed'}}).status_code == 200
    chat = client.post(f"/v1/runs/{parent['id']}/chat",
        json={'message': '继续最新运行', 'action': 'resume'})
    assert chat.status_code == 202
    job = client.post('/internal/worker/claim', json={'worker_id': worker_id}, headers=headers).json()['run']
    assert job['summary']['action']['type'] == 'resume'
    assert job['summary']['action']['parent_run_id'] == child['id']


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


def test_mixed_run_is_claimed_only_by_mixed_worker(client):
    run = submit(client, backend="mixed")
    headers, codex_worker = worker(client)
    assert client.post("/internal/worker/claim", json={"worker_id": codex_worker}, headers=headers).json()["run"] is None
    mixed_worker = "c" * 32
    response = client.post("/internal/worker/heartbeat", json={"worker_id": mixed_worker,
        "backends": ["codex", "llm", "mixed", "prd_codex", "prd_llm"]}, headers=headers)
    assert response.status_code == 200
    claimed = client.post("/internal/worker/claim", json={"worker_id": mixed_worker}, headers=headers).json()["run"]
    assert claimed["id"] == run["id"]


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
