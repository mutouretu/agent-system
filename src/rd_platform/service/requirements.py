"""Versioned PRD storage and atomic handoff to the existing durable queue."""
import json
import time
import uuid
from typing import Literal
from fastapi import HTTPException
from pydantic import Field, model_validator
from sqlalchemy import Column, ForeignKey, JSON, String, Table, select, func
from rd_platform.agents.dev.schemas import Contract
from rd_platform.agents.dev.requirements import RequirementDocument, FeaturePackage, Short, RequirementReview
from .run_store import metadata, projects, runs, StoreConflict

requirements = Table('workbench_requirements', metadata,
    Column('id', String(32), primary_key=True),
    Column('project_id', ForeignKey(projects.c.id), nullable=False),
    Column('payload', JSON, nullable=False))
work_items = Table('workbench_work_items', metadata,
    Column('id', String(32), primary_key=True),
    Column('project_id', ForeignKey(projects.c.id), nullable=False),
    Column('requirement_id', ForeignKey(requirements.c.id), nullable=False),
    Column('payload', JSON, nullable=False))


class WorkItemCreate(Contract):
    kind: Literal['FEATURE', 'CHANGE', 'DEBUG']
    title: str = Field(min_length=1, max_length=256, pattern=r'\S')
    description: str = Field(min_length=1, max_length=16000, pattern=r'\S')
    acceptance_criteria: list[Short] = Field(default_factory=list, max_length=50)


class RequirementChat(Contract):
    message: str = Field(min_length=1, max_length=4000, pattern=r'\S')


def ensure_legacy_requirement(db, project_id):
    for payload, in db.execute(select(requirements.c.payload).where(requirements.c.project_id == project_id)):
        if payload.get('kind') == 'legacy':
            return payload['id']
    req_id = uuid.uuid4().hex
    payload = {'id': req_id, 'project_id': project_id, 'revision': 0,
               'kind': 'legacy', 'document': {'title': '历史运行（待整理）', 'business_context': '',
               'feature_packages': [], 'acceptance_criteria': []}, 'accepted': [],
               'messages': [], 'run_id': None, 'analysis_id': None}
    db.execute(requirements.insert().values(id=req_id, project_id=project_id, payload=payload))
    return req_id


def insert_work_item(db, project_id, req_id, kind, title, description, criteria=None, source_run_id=None):
    item_id = uuid.uuid4().hex
    payload = {'id': item_id, 'project_id': project_id, 'requirement_id': req_id,
               'kind': kind, 'title': title, 'description': description,
               'acceptance_criteria': criteria or [], 'created_at': time.time()}
    if source_run_id:
        payload['source_run_id'] = source_run_id
    db.execute(work_items.insert().values(id=item_id, project_id=project_id,
                                         requirement_id=req_id, payload=payload))
    return payload


class DraftDocument(Contract):
    title: str = Field(default='', max_length=512)
    business_context: str = Field(default='', max_length=4000)
    feature_packages: list[FeaturePackage] = Field(default_factory=list, max_length=50)
    acceptance_criteria: list[Short] = Field(default_factory=list, max_length=100)


class Draft(Contract):
    revision: int = Field(ge=0)
    document: DraftDocument
    applied_analysis_id: str | None = Field(default=None, pattern=r'^[a-f0-9]{32}$')
    ai_review: RequirementReview | None = None
    accepted: list[str] = Field(default_factory=list, max_length=100)
    messages: list[dict[str, str]] = Field(default_factory=list, max_length=30)

    @model_validator(mode='after')
    def bounded_messages(self):
        if any(set(m) != {'role', 'content'} or m['role'] not in ('user', 'architect') or len(m['content']) > 16000 for m in self.messages):
            raise ValueError('invalid conversation messages')
        if not set(self.accepted) <= set(self.document.acceptance_criteria):
            raise ValueError('accepted items must be defined criteria')
        return self


class Submit(Draft):
    backend: Literal['codex', 'llm', 'mixed'] = 'codex'

    @model_validator(mode='after')
    def ready(self):
        RequirementDocument.model_validate(self.document.model_dump())
        return self


class Analyze(Contract):
    revision: int = Field(ge=0)
    prompt: str = Field(min_length=1, max_length=16000, pattern=r'\S')
    document: DraftDocument = Field(default_factory=DraftDocument)
    messages: list[dict[str, str]] = Field(default_factory=list, max_length=30)
    backend: Literal['codex', 'llm'] = 'codex'


def install_requirements(app):
    store = lambda: app.state.run_store

    def get(db, project_id, req_id, lock=False):
        query = select(requirements).where(requirements.c.id == req_id, requirements.c.project_id == project_id)
        row = db.execute(query.with_for_update() if lock else query).mappings().first()
        if not row:
            raise HTTPException(404, 'requirement not found')
        return row['payload']

    def enqueue(db, project_id, text, backend, template='custom'):
        if len(text) > 16000:
            raise HTTPException(422, '需求上下文超过 16000 字符，请精简后重试')
        if db.scalar(select(func.count()).select_from(runs).where(runs.c.status.in_(['QUEUED', 'RUNNING']))) >= 100:
            raise HTTPException(409, 'active run capacity reached')
        now = time.time()
        record = dict(id=uuid.uuid4().hex, project_id=project_id, status='QUEUED', backend=backend,
                      requirements=text, template=template, max_retries=3, created_at=now, updated_at=now,
                      generation=0, tasks=[], summary={})
        db.execute(runs.insert().values(**record))
        store().event(db, record['id'], 'run_queued', {'status': 'QUEUED', 'backend': backend})
        return record

    @app.post('/v1/projects/{project_id}/requirements', status_code=201)
    def create(project_id: str):
        with store().transaction() as db:
            if not db.scalar(select(projects.c.id).where(projects.c.id == project_id).with_for_update()):
                raise HTTPException(404, 'project not found')
            if db.scalar(select(func.count()).select_from(requirements).where(requirements.c.project_id == project_id)) >= 100:
                raise HTTPException(409, 'requirement capacity reached')
            req_id = uuid.uuid4().hex
            payload = {'id': req_id, 'project_id': project_id, 'revision': 0, 'document': None,
                       'accepted': [], 'messages': [], 'run_id': None, 'analysis_id': None}
            db.execute(requirements.insert().values(id=req_id, project_id=project_id, payload=payload))
            return payload

    @app.get('/v1/projects/{project_id}/requirements')
    def listing(project_id: str):
        with store().transaction() as db:
            if not db.scalar(select(projects.c.id).where(projects.c.id == project_id)):
                raise HTTPException(404, 'project not found')
            return {'requirements': [row[0] for row in db.execute(select(requirements.c.payload).where(requirements.c.project_id == project_id))]}

    @app.post('/v1/projects/{project_id}/requirements/{req_id}/work-items', status_code=201)
    def create_work_item(project_id: str, req_id: str, request: WorkItemCreate):
        with store().transaction() as db:
            get(db, project_id, req_id, True)
            if db.scalar(select(func.count()).select_from(work_items).where(
                    work_items.c.requirement_id == req_id)) >= 100:
                raise HTTPException(409, 'work item capacity reached')
            return insert_work_item(db, project_id, req_id, request.kind, request.title.strip(),
                                    request.description.strip(), request.acceptance_criteria)

    @app.get('/v1/projects/{project_id}/requirements/{req_id}/work-items')
    def list_work_items(project_id: str, req_id: str):
        with store().transaction() as db:
            get(db, project_id, req_id)
            items = [row[0] for row in db.execute(select(work_items.c.payload).where(
                work_items.c.project_id == project_id,
                work_items.c.requirement_id == req_id))]
            related = db.execute(select(runs.c.id, runs.c.status, runs.c.created_at, runs.c.summary).where(
                runs.c.project_id == project_id, runs.c.template.notin_(['prd', 'chat']),
                runs.c.summary['requirement_id'].as_string() == req_id)).all()
            for item in items:
                history = [(run_id, status, created) for run_id, status, created, summary in related
                           if summary.get('work_item_id') == item['id']]
                history.sort(key=lambda row: row[2], reverse=True)
                item['run_ids'] = [row[0] for row in history]
                item['status'] = history[0][1] if history else 'PLANNED'
            return {'work_items': sorted(items, key=lambda item: item['created_at'], reverse=True)}

    @app.get('/v1/projects/{project_id}/requirements/{req_id}/chat')
    def requirement_chat(project_id: str, req_id: str):
        try:
            return {'messages': store().get_requirement_chat(project_id, req_id)}
        except KeyError:
            raise HTTPException(404, 'requirement not found') from None

    @app.post('/v1/projects/{project_id}/requirements/{req_id}/chat', status_code=202)
    def send_requirement_chat(project_id: str, req_id: str, request: RequirementChat):
        try:
            return store().create_requirement_chat(project_id, req_id, request.message.strip())
        except KeyError:
            raise HTTPException(404, 'requirement not found') from None
        except StoreConflict as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get('/v1/projects/{project_id}/requirements/{req_id}')
    def detail(project_id: str, req_id: str):
        with store().transaction() as db:
            return get(db, project_id, req_id)

    def save(project_id, req_id, request, submit=False):
        with store().transaction() as db:
            project = db.scalar(select(projects.c.payload).where(projects.c.id == project_id).with_for_update())
            old = get(db, project_id, req_id, True)
            if request.revision != old['revision']:
                raise HTTPException(409, '需求已更新，请重新打开后编辑')
            if submit and old.get('submitted_revision') == old['revision']:
                raise HTTPException(409, '此需求版本已提交；请先保存修改后的新版本')
            payload = {**old, **request.model_dump(exclude={'backend'}), 'revision': old['revision'] + 1}
            document = payload['document']
            context = dict(project.get('context', {}))
            workspace = dict(context.get('workspace', {'revision': 0, 'milestones': [], 'accepted': []}))
            if submit:
                text = json.dumps(document, ensure_ascii=False)
                item = insert_work_item(db, project_id, req_id, 'FEATURE', document['title'], text,
                                        document['acceptance_criteria'])
                run = enqueue(db, project_id, text, request.backend)
                db.execute(runs.update().where(runs.c.id == run['id']).values(
                    summary={'requirement_id': req_id, 'work_item_id': item['id']}))
                payload.update(run_id=run['id'], work_item_id=item['id'],
                               submitted_revision=payload['revision'])
                milestones = [m for m in workspace['milestones'] if m.get('run_id') != old.get('run_id') or not old.get('run_id')]
                milestones.append({'name': document['title'][:128], 'goal': document['business_context'],
                                   'packages': [p['name'] for p in document['feature_packages']], 'run_id': run['id']})
                if len(milestones) > 50:
                    raise HTTPException(409, 'milestone capacity reached')
                workspace['milestones'] = milestones
            context['active_requirement'] = {'id': req_id, 'revision': payload['revision'], 'document': document,
                                             'accepted': payload['accepted'], 'run_id': payload.get('run_id')}
            workspace['revision'] += 1
            context['workspace'] = workspace
            db.execute(projects.update().where(projects.c.id == project_id).values(payload={**project, 'context': context}))
            db.execute(requirements.update().where(requirements.c.id == req_id).values(payload=payload))
            return payload

    @app.put('/v1/projects/{project_id}/requirements/{req_id}')
    def save_draft(project_id: str, req_id: str, request: Draft):
        return save(project_id, req_id, request)

    @app.post('/v1/projects/{project_id}/requirements/{req_id}/submit', status_code=202)
    def submit(project_id: str, req_id: str, request: Submit):
        return save(project_id, req_id, request, True)

    @app.post('/v1/projects/{project_id}/requirements/{req_id}/analyze', status_code=202)
    def analyze(project_id: str, req_id: str, request: Analyze):
        with store().transaction() as db:
            old = get(db, project_id, req_id, True)
            if old['revision'] != request.revision:
                raise HTTPException(409, '需求已更新，请重新打开后重试')
            if old.get('analysis_id'):
                active = db.scalar(select(runs.c.status).where(runs.c.id == old['analysis_id']))
                if active in ('QUEUED', 'RUNNING'):
                    raise HTTPException(409, 'Architect 正在处理此需求')
            if not any('prd_' + request.backend in worker['backends'] for worker in store().worker_status()):
                raise HTTPException(503, '需求 Architect Worker 未就绪，请启动更新后的本地 Worker')
            text = json.dumps(request.model_dump(exclude={'backend', 'revision'}), ensure_ascii=False)
            run = enqueue(db, project_id, text, 'prd_' + request.backend, 'prd')
            db.execute(runs.update().where(runs.c.id == run['id']).values(summary={'requirement_id': req_id}))
            db.execute(requirements.update().where(requirements.c.id == req_id).values(payload={**old, 'analysis_id': run['id'],
                'document': request.document.model_dump(), 'messages': (request.messages + [{'role': 'user', 'content': request.prompt}])[-30:]}))
            return {'run_id': run['id']}
