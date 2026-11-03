"""Project → requirement → work item boundaries and durable discussion."""

from tests.service.test_workbench import client, settings, worker
from rd_platform.service.run_store import RunStore, runs
from sqlalchemy import select


def test_work_item_run_and_requirement_conversation(client):
    project = client.post('/v1/projects', json={'name': '研发控制台'}).json()
    base = f"/v1/projects/{project['id']}/requirements"
    req = client.post(base).json()
    path = f"{base}/{req['id']}"
    doc = {'title': 'Agent 管理', 'business_context': '统一配置角色',
           'feature_packages': [], 'acceptance_criteria': []}
    assert client.put(path, json={'revision': 0, 'document': doc}).status_code == 200
    item = client.post(path + '/work-items', json={'kind': 'CHANGE',
        'title': '移除重复面板', 'description': '保留独立 Agent 页面，移除项目开发内旧面板',
        'acceptance_criteria': ['项目开发不再显示旧面板']}).json()
    assert item['requirement_id'] == req['id'] and item['kind'] == 'CHANGE'
    other_req = client.post(f"/v1/projects/{project['id']}/requirements", json={}).json()
    run_response = client.post('/v1/runs', json={'project_id': project['id'],
        'work_item_id': item['id'], 'requirements': item['description'], 'backend': 'codex'})
    assert run_response.status_code == 202, run_response.text
    run = run_response.json()
    assert run['summary']['requirement_id'] == req['id']
    assert run['summary']['work_item_id'] == item['id']
    assert client.post('/v1/runs', json={'project_id': project['id'],
        'work_item_id': '0' * 32, 'requirements': 'wrong'}).status_code == 409
    other_project = client.post('/v1/projects', json={'name': 'unrelated'}).json()
    assert client.post('/v1/runs', json={'project_id': other_project['id'],
        'work_item_id': item['id'], 'requirements': 'wrong'}).status_code == 409
    listed = client.get(path + '/work-items').json()['work_items']
    assert listed[0]['run_ids'] == [run['id']]
    assert listed[0]['status'] == 'QUEUED'

    question = client.post(path + '/chat', json={'message': '这项变更的进展？'})
    assert question.status_code == 202, question.text
    assert client.post(path + '/chat', json={'message': '再问一次'}).status_code == 409
    headers, wid = worker(client)
    claimed = client.post('/internal/worker/claim', json={'worker_id': wid}, headers=headers).json()['run']
    assert claimed['id'] == run['id']
    assert client.post(f"/internal/worker/runs/{run['id']}/finish", headers=headers,
        json={'worker_id': wid, 'generation': claimed['generation'],
              'summary': {'status': 'PASS', 'execution_mode': 'docker',
                          'final_test_summary': {'outcome': 'PASSED'}}}).status_code == 200
    chat = client.post('/internal/worker/claim', json={'worker_id': wid}, headers=headers).json()['run']
    assert chat['id'] == question.json()['id']
    assert '移除重复面板' in chat['summary']['chat_context']
    assert chat['summary']['requirement_id'] == req['id']
    assert client.post(f"/internal/worker/runs/{chat['id']}/finish", headers=headers,
        json={'worker_id': wid, 'generation': chat['generation'],
              'summary': {'status': 'CHAT_READY', 'answer': '已完成。'}}).status_code == 200
    messages = client.get(path + '/chat').json()['messages']
    assert [(msg['message'], msg['answer']) for msg in messages] == [('这项变更的进展？', '已完成。')]
    assert client.get(f"{base}/{other_req['id']}/chat").json()['messages'] == []
    assert client.get(path + '/work-items').json()['work_items'][0]['status'] == 'COMPLETED'
    second = client.post(path + '/work-items', json={'kind': 'DEBUG',
        'title': '排查页面刷新', 'description': '确认刷新后仍可进入开发项'}).json()
    assert second['requirement_id'] == req['id']
    followup = client.post(path + '/chat', json={'message': '新 Debug 与之前的变更有何关系？'})
    assert followup.status_code == 202
    next_chat = client.post('/internal/worker/claim', json={'worker_id': wid}, headers=headers).json()['run']
    assert next_chat['id'] == followup.json()['id']
    assert '移除重复面板' in next_chat['summary']['chat_context']
    assert '排查页面刷新' in next_chat['summary']['chat_context']
    assert '已完成。' in next_chat['summary']['chat_context']


def test_legacy_runs_are_backfilled_without_losing_evidence(settings):
    url = f"sqlite:///{settings.data / 'hierarchy.sqlite3'}"
    store = RunStore(url)
    project = {'id': 'a' * 32, 'name': 'legacy', 'context': {}, 'tasks': []}
    store.save_project(project)
    run = store.create_run(project['id'], 'codex', 'implement cache')
    with store.transaction() as db:
        old = db.execute(select(runs).where(runs.c.id == run['id'])).mappings().one()
        db.execute(runs.update().where(runs.c.id == run['id']).values(
            summary={'status': 'PASS', 'evidence_dir': '/evidence/old'}))
        assert old['summary']['work_item_id']
    store.close()
    reopened = RunStore(url)
    recovered = reopened.get_run(run['id'])
    assert recovered['summary']['evidence_dir'] == '/evidence/old'
    assert recovered['summary']['work_item_id']
    assert recovered['summary']['requirement_id']
    first = recovered['summary']['work_item_id']
    reopened.close()
    again = RunStore(url)
    assert again.get_run(run['id'])['summary']['work_item_id'] == first
    again.close()
