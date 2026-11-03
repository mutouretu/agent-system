"""PRD persistence, project scope, strict generation and atomic handoff."""
import pytest
from tests.service.test_workbench import client, settings, worker


def document():
    return {'title': '验证需求', 'business_context': '构建可验证的标准库功能',
            'feature_packages': [{'id': 'PKG-1', 'name': '核心功能', 'description': '处理空输入', 'group': '核心', 'priority': 'P0'}],
            'acceptance_criteria': ['空输入返回空结果']}


def draft(client):
    project = client.post('/v1/projects', json={'name': 'PRD test'}).json()
    req = client.post(f"/v1/projects/{project['id']}/requirements").json()
    return project, req, f"/v1/projects/{project['id']}/requirements/{req['id']}"


def test_prd_save_scope_conflict_and_submit(client):
    project, req, path = draft(client)
    payload = {'revision': 0, 'document': document(), 'accepted': [], 'messages': []}
    saved = client.put(path, json=payload)
    assert saved.status_code == 200, saved.text
    assert client.put(path, json=payload).status_code == 409
    assert client.get(path.replace(project['id'], '0'*32)).status_code == 404
    payload['revision'] = 1
    payload['accepted'] = ['空输入返回空结果']
    result = client.post(path+'/submit', json=payload)
    assert result.status_code == 202, result.text
    run = client.get('/v1/runs/'+result.json()['run_id']).json()
    assert run['template'] == 'custom' and run['status'] == 'QUEUED'
    assert run['summary']['requirement_id'] == req['id']
    assert run['summary']['work_item_id'] == result.json()['work_item_id']
    items = client.get(path+'/work-items').json()['work_items']
    assert len(items) == 1 and items[0]['kind'] == 'FEATURE'
    context = client.get('/v1/projects/'+project['id']).json()['context']
    assert context['workspace']['milestones'][0]['run_id'] == run['id']
    assert context['active_requirement']['accepted'] == payload['accepted']
    payload['revision'] = 2
    assert client.post(path+'/submit', json=payload).status_code == 409
    assert len(client.get('/v1/runs').json()['runs']) == 1


def test_prd_draft_can_be_incomplete_but_execution_cannot(client):
    _, _, path = draft(client)
    payload = {'revision': 0, 'document': {}, 'messages': [{'role': 'user', 'content': '原始需求草案'}]}
    assert client.put(path, json=payload).status_code == 200
    payload['revision'] = 1
    assert client.post(path+'/submit', json=payload).status_code == 422
    assert client.get('/v1/runs').json()['runs'] == []


def test_analysis_worker_contract_and_non_execution_evidence(client):
    project, _, path = draft(client)
    payload = {'revision': 0, 'prompt': '解析需求', 'document': document()}
    assert client.post(path+'/analyze', json=payload).status_code == 503
    headers, wid = worker(client)
    client.post('/internal/worker/heartbeat', json={'worker_id': wid, 'backends': ['prd_codex']}, headers=headers)
    response = client.post(path+'/analyze', json=payload)
    assert response.status_code == 202, response.text
    assert client.post(path+'/analyze', json=payload).status_code == 409
    run = client.post('/internal/worker/claim', json={'worker_id': wid}, headers=headers).json()['run']
    assert run['template'] == 'prd'
    assert client.get('/v1/runs').json()['runs'] == []
    endpoint = '/internal/worker/runs/'+run['id']+'/finish'
    result = {'worker_id': wid, 'generation': run['generation'], 'summary': {'status': 'PRD_READY', 'prd': {}}}
    assert client.post(endpoint, json=result, headers=headers).status_code == 422
    result['summary']['prd'] = {'document': document(), 'completeness_score': 80, 'risks': ['需要超时定义'], 'reply': '已完成拆解'}
    assert client.post(endpoint, json=result, headers=headers).status_code == 200
    assert client.get('/v1/runs/'+run['id']).json()['status'] == 'COMPLETED'
