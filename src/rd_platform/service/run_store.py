"""Persistent local workbench queue. Production claims use PostgreSQL SKIP LOCKED."""

from contextlib import contextmanager
import json
import time
import uuid

from sqlalchemy import (JSON, Column, Float, ForeignKey, Integer, MetaData, String, Table,
                        Text, create_engine, func, select, update)
from rd_platform.agent_management import default_agent_config

metadata = MetaData()
projects = Table("workbench_projects", metadata,
    Column("id", String(32), primary_key=True), Column("payload", JSON, nullable=False),
    Column("created_at", Float, nullable=False))
runs = Table("workbench_runs", metadata,
    Column("id", String(32), primary_key=True), Column("project_id", ForeignKey(projects.c.id), nullable=False),
    Column("status", String(24), nullable=False), Column("backend", String(16), nullable=False),
    Column("requirements", Text, nullable=False), Column("template", String(16), nullable=False),
    Column("max_retries", Integer, nullable=False), Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False), Column("generation", Integer, nullable=False, default=0),
    Column("worker_id", String(64)), Column("lease_until", Float),
    Column("tasks", JSON, nullable=False), Column("summary", JSON, nullable=False))
events = Table("workbench_events", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("run_id", ForeignKey(runs.c.id), nullable=False, index=True),
    Column("kind", String(32), nullable=False), Column("data", JSON, nullable=False),
    Column("created_at", Float, nullable=False))
workers = Table("workbench_workers", metadata,
    Column("id", String(64), primary_key=True), Column("last_seen", Float, nullable=False),
    Column("backends", JSON, nullable=False))
TERMINAL = {"COMPLETED", "FAILED", "INTERRUPTED"}
LEASE_SECONDS = 90


def chat_task_evidence(task):
    """Bounded role evidence for Team Lead; source files stay in artifact storage."""
    output = task.get('output') or {}
    role = task.get('capability')
    if role == 'coding':
        result = {'files': list((output.get('files') or {}).keys()),
                  'tests': list((output.get('tests') or {}).keys()),
                  'commit_message': output.get('commit_message')}
    elif role == 'testing':
        logs = output.get('evidence_logs') or {}
        result = {'test_summary': output.get('test_summary'),
                  'evidence_logs': {key: logs.get(key) for key in ('execution_mode', 'exit_code', 'run_id')},
                  'test_logs': str(logs.get('logs') or '')[:8000]}
    else:
        result = output
    return {'id': task.get('id'), 'role': role, 'status': task.get('status'),
            'error': str(task.get('error') or '')[:8000], 'output': result}


class StoreConflict(ValueError):
    pass


class RunStore:
    def __init__(self, url):
        self.engine = create_engine(url, pool_pre_ping=True)
        metadata.create_all(self.engine)

    @contextmanager
    def transaction(self):
        with self.engine.begin() as connection:
            yield connection

    def close(self):
        self.engine.dispose()

    @staticmethod
    def event(db, run_id, kind, data):
        db.execute(events.insert().values(run_id=run_id, kind=kind, data=data, created_at=time.time()))

    def save_project(self, project):
        with self.transaction() as db:
            existing = db.scalar(select(projects.c.payload).where(projects.c.id == project["id"]).with_for_update())
            if existing is not None:
                protected = {key: existing['context'][key] for key in ('workspace', 'active_requirement', 'agent_config') if key in existing.get('context', {})}
                project = {**project, 'context': {**project.get('context', {}), **protected}}
                db.execute(projects.update().where(projects.c.id == project["id"]).values(payload=project))
            else:
                if db.scalar(select(func.count()).select_from(projects)) >= 1000:
                    raise StoreConflict("project capacity reached (1000)")
                db.execute(projects.insert().values(id=project["id"], payload=project, created_at=time.time()))

    def list_projects(self):
        with self.transaction() as db:
            return [row.payload for row in db.execute(select(projects).order_by(projects.c.created_at.desc()))]

    def get_project(self, project_id):
        with self.transaction() as db:
            return db.scalar(select(projects.c.payload).where(projects.c.id == project_id))

    def get_agent_config(self, project_id):
        project = self.get_project(project_id)
        if project is None:
            raise KeyError(project_id)
        return project.get("context", {}).get("agent_config") or default_agent_config()

    def update_agent_config(self, project_id, revision, agents):
        with self.transaction() as db:
            project = db.scalar(select(projects.c.payload).where(projects.c.id == project_id).with_for_update())
            if project is None:
                raise KeyError(project_id)
            old = project.get("context", {}).get("agent_config") or default_agent_config()
            if old["revision"] != revision:
                raise StoreConflict("Agent 配置已更新，请刷新后重试")
            saved = {"revision": revision + 1, "agents": agents}
            project = {**project, "context": {**project.get("context", {}), "agent_config": saved}}
            db.execute(projects.update().where(projects.c.id == project_id).values(payload=project))
            return saved

    def update_workspace(self, project_id, workspace):
        with self.transaction() as db:
            project = db.scalar(select(projects.c.payload).where(projects.c.id == project_id).with_for_update())
            if project is None:
                raise KeyError(project_id)
            previous = project.get("context", {}).get("workspace", {})
            if workspace["revision"] != previous.get("revision", 0):
                raise StoreConflict("项目已被更新，请刷新后重试")
            for milestone in workspace["milestones"]:
                if milestone.get("run_id") and not db.scalar(select(runs.c.id).where(
                    runs.c.id == milestone["run_id"], runs.c.project_id == project_id
                )):
                    raise StoreConflict("关联的运行不属于此项目")
            workspace = {**workspace, "revision": workspace["revision"] + 1}
            project = {**project, "context": {**project.get("context", {}), "workspace": workspace}}
            db.execute(projects.update().where(projects.c.id == project_id).values(payload=project))
            return project

    def expire(self, db):
        stale = db.execute(select(runs.c.id, runs.c.summary).where(runs.c.status == "RUNNING", runs.c.lease_until < time.time())
                           .with_for_update(skip_locked=True)).all()
        for run_id, previous in stale:
            summary = {**{key: previous[key] for key in ('agent_config', 'parent_run_id', 'workspace_mode') if key in previous},
                       "status": "FAILED", "error": "Worker lease expired; inspect evidence and resume from a checkpoint"}
            db.execute(runs.update().where(runs.c.id == run_id).values(status="INTERRUPTED", summary=summary,
                       updated_at=time.time(), generation=runs.c.generation + 1))
            self.event(db, run_id, "run_finished", {"status": "INTERRUPTED", "error": summary["error"]})

    def create_run(self, project_id, backend, requirements, template="custom", max_retries=3,
                   workspace_mode='isolated'):
        now = time.time()
        record = dict(id=uuid.uuid4().hex, project_id=project_id, status="QUEUED", backend=backend,
                      requirements=requirements, template=template, max_retries=max_retries,
                      created_at=now, updated_at=now, generation=0, tasks=[], summary={})
        with self.transaction() as db:
            project = db.scalar(select(projects.c.payload).where(projects.c.id == project_id).with_for_update())
            if project is None:
                raise KeyError(project_id)
            if workspace_mode not in ('isolated', 'repository') or (template != 'custom' and workspace_mode == 'repository'):
                raise StoreConflict('仅自定义研发任务支持主项目工作区模式')
            record['summary'] = {'workspace_mode': workspace_mode}
            if backend == "configured":
                config = project.get("context", {}).get("agent_config")
                if not config:
                    raise StoreConflict("请先保存 Agent 配置")
                record["summary"]["agent_config"] = config
            if db.scalar(select(func.count()).select_from(runs).where(runs.c.status.in_(["QUEUED", "RUNNING"]))) >= 100:
                raise StoreConflict("active run capacity reached (100)")
            db.execute(runs.insert().values(**record))
            self.event(db, record["id"], "run_queued", {"status": "QUEUED", "backend": backend})
        return self.get_run(record["id"])

    def resume_run(self, parent_id, task_id=None):
        """Queue a child run at a selected role checkpoint, replaying earlier evidence."""
        now = time.time()
        with self.transaction() as db:
            parent = db.execute(select(runs).where(runs.c.id == parent_id).with_for_update()).mappings().first()
            if parent is None:
                raise KeyError(parent_id)
            if parent['template'] not in ('custom', 'resume') or parent['status'] not in ('FAILED', 'INTERRUPTED'):
                raise StoreConflict('仅支持从失败或中断的研发运行恢复')
            tasks = parent['tasks']
            if not tasks:
                raise StoreConflict('此运行没有可恢复的角色检查点')
            selected = task_id or next((task['id'] for task in reversed(tasks)
                                        if task['status'] in ('FAILED', 'BLOCKED')), None)
            indices = [index for index, task in enumerate(tasks) if task['id'] == selected]
            if len(indices) != 1:
                raise StoreConflict('所选角色节点不属于此运行')
            prefix = tasks[:indices[0]]
            from rd_platform.agents.dev.schemas import ArchitectArtifacts, CoderArtifacts, ReviewerArtifacts, TesterArtifacts
            from pydantic import ValidationError
            try:
                validators = {'architecture': ArchitectArtifacts, 'coding': CoderArtifacts,
                              'review': ReviewerArtifacts, 'testing': TesterArtifacts}
                checked = []
                for task in prefix:
                    if task['capability'] not in validators:
                        raise ValueError('unknown role')
                    if task['status'] not in ('COMPLETED', 'FAILED', 'BLOCKED'):
                        raise ValueError('cannot reuse an unfinished role')
                    output = task.get('output')
                    if task['status'] == 'COMPLETED' and not output:
                        raise ValueError('missing completed output')
                    if output is not None:
                        output = validators[task['capability']].model_validate(output).model_dump(mode='json')
                    checked.append({**task, 'output': output})
            except (ValidationError, ValueError, KeyError):
                raise StoreConflict('所选节点之前的产物不符合恢复契约') from None
            children = db.execute(select(runs.c.status, runs.c.summary).where(
                runs.c.project_id == parent['project_id'], runs.c.template == 'resume')).all()
            if any(status in ('QUEUED', 'RUNNING') and summary.get('parent_run_id') == parent_id for status, summary in children):
                raise StoreConflict('此运行已有正在执行的恢复任务')
            if db.scalar(select(func.count()).select_from(runs).where(runs.c.status.in_(['QUEUED', 'RUNNING']))) >= 100:
                raise StoreConflict('active run capacity reached (100)')
            snapshot = parent['summary'].get('agent_config')
            if parent['backend'] == 'configured' and not snapshot:
                project = db.scalar(select(projects.c.payload).where(projects.c.id == parent['project_id']))
                snapshot = project.get('context', {}).get('agent_config') if project else None
                if not snapshot:
                    raise StoreConflict('缺少项目 Agent 配置，无法恢复')
            summary = {'parent_run_id': parent_id, 'resume_from_task': selected,
                       'resume_seed': {'parent_run_id': parent_id, 'target_task_id': selected, 'tasks': checked}}
            summary['workspace_mode'] = parent['summary'].get('workspace_mode', 'isolated')
            if snapshot:
                summary['agent_config'] = snapshot
            record = dict(id=uuid.uuid4().hex, project_id=parent['project_id'], status='QUEUED',
                          backend=parent['backend'], requirements=parent['requirements'], template='resume',
                          max_retries=parent['max_retries'], created_at=now, updated_at=now, generation=0,
                          tasks=[], summary=summary)
            db.execute(runs.insert().values(**record))
            self.event(db, record['id'], 'run_queued', {'status': 'QUEUED', 'parent_run_id': parent_id,
                                                       'resume_from_task': selected})
        return self.get_run(record['id'])

    def list_runs(self, project_id=None):
        with self.transaction() as db:
            self.expire(db)
            query = select(*(column for column in runs.c if column.name not in ("tasks", "requirements"))).where(runs.c.template.notin_(['prd', 'chat'])).order_by(runs.c.created_at.desc()).limit(200)
            if project_id:
                query = query.where(runs.c.project_id == project_id)
            return [self.public(dict(row), compact=True) for row in db.execute(query).mappings()]

    @staticmethod
    def public(record, compact=False):
        record.pop("lease_until", None)
        record.pop("worker_id", None)
        record['summary'] = {key: value for key, value in record['summary'].items() if key != 'resume_seed'}
        if compact:
            record.pop("tasks", None)
            record.pop("requirements", None)
            record["summary"] = {key: value for key, value in record["summary"].items()
                                 if key in ("status", "error", "iteration_count", "final_test_summary", "elapsed_seconds",
                                            "parent_run_id", "resume_from_task", "acceptance", "workspace_mode", "delivery")}
        return record

    def get_run(self, run_id):
        with self.transaction() as db:
            self.expire(db)
            row = db.execute(select(runs).where(runs.c.id == run_id)).mappings().first()
            if row is None:
                raise KeyError(run_id)
            return self.public(dict(row))

    @staticmethod
    def _chat_rows(db, parent_id):
        rows = db.execute(select(runs).where(runs.c.template == 'chat',
            runs.c.summary['parent_run_id'].as_string() == parent_id)
            .order_by(runs.c.created_at, runs.c.id).limit(50)).mappings().all()
        return [dict(row) for row in rows]

    @staticmethod
    def _related_resume_rows(db, parent):
        found, frontier = [], [parent['id']]
        for _ in range(4):
            rows = db.execute(select(runs).where(runs.c.project_id == parent['project_id'],
                runs.c.template == 'resume', runs.c.summary['parent_run_id'].as_string().in_(frontier))
                .order_by(runs.c.created_at.desc()).limit(12)).mappings().all()
            if not rows:
                break
            found.extend(dict(row) for row in rows)
            frontier = [row['id'] for row in rows]
        return sorted(found, key=lambda row: row['created_at'], reverse=True)[:4]

    def get_chat(self, parent_id):
        with self.transaction() as db:
            parent = db.execute(select(runs.c.id, runs.c.template).where(runs.c.id == parent_id)).first()
            if parent is None or parent.template in ('chat', 'prd'):
                raise KeyError(parent_id)
            return [{"id": row['id'], "message": row['requirements'], "status": row['status'],
                     "answer": row['summary'].get('answer'), "error": row['summary'].get('error'),
                     "resume_run_id": row['summary'].get('resume_run_id'),
                     "created_at": row['created_at']} for row in self._chat_rows(db, parent_id)]

    def create_chat(self, parent_id, message, action='discuss', task_id=None):
        now = time.time()
        with self.transaction() as db:
            parent = db.execute(select(runs).where(runs.c.id == parent_id).with_for_update()).mappings().first()
            if parent is None or parent['template'] in ('chat', 'prd'):
                raise KeyError(parent_id)
            history = self._chat_rows(db, parent_id)
            if any(row['status'] in ('QUEUED', 'RUNNING') for row in history):
                raise StoreConflict('上一条讨论仍在处理，请等待回复')
            if len(history) >= 50:
                raise StoreConflict('本轮讨论已达 50 条上限')
            requested_action = None
            related = self._related_resume_rows(db, parent)
            if action == 'resume':
                current = related[0] if related else parent
                if current['status'] in ('QUEUED', 'RUNNING', 'COMPLETED'):
                    requested_action = {'type': 'report_existing', 'run_id': current['id'],
                                        'status': current['status'],
                                        'final_test_summary': current['summary'].get('final_test_summary'),
                                        'acceptance': current['summary'].get('acceptance')}
                elif current['status'] not in ('FAILED', 'INTERRUPTED'):
                    raise StoreConflict('当前运行尚未失败或中断，无需恢复')
                else:
                    requested_action = {'type': 'resume', 'parent_run_id': current['id'], 'task_id': task_id}
            elif action != 'discuss' or task_id is not None:
                raise StoreConflict('无效的会话动作')
            if db.scalar(select(func.count()).select_from(runs).where(runs.c.status.in_(['QUEUED', 'RUNNING']))) >= 100:
                raise StoreConflict('active run capacity reached (100)')
            related_context = [{'run_id': row['id'], 'status': row['status'],
                'resume_from_task': row['summary'].get('resume_from_task'),
                'error': str(row['summary'].get('error') or '')[:4000],
                'acceptance': row['summary'].get('acceptance'),
                'final_test_summary': row['summary'].get('final_test_summary'),
                'tasks': [chat_task_evidence(task) for task in row['tasks'][-6:]]}
                for row in related]
            context = {"related_runs_newest_first": related_context,
                       "original_run_id": parent_id,
                       "original_requirement": parent['requirements'][:12000],
                       "run_status": parent['status'], "run_error": str(parent['summary'].get('error') or '')[:8000],
                       "tasks": [chat_task_evidence(task) for task in parent['tasks'][-8:]],
                       "discussion": [{"question": item['requirements'][:2000],
                                       "answer": str(item['summary'].get('answer') or '')[:4000]}
                                      for item in history[-8:]]}
            context_text = json.dumps(context, ensure_ascii=False)[:48000]
            snapshot = parent['summary'].get('agent_config')
            if parent['backend'] == 'configured' and not snapshot:
                project = db.scalar(select(projects.c.payload).where(projects.c.id == parent['project_id']))
                snapshot = project.get('context', {}).get('agent_config') if project else None
            summary = {"parent_run_id": parent_id, "chat_context": context_text}
            if requested_action:
                summary['action'] = requested_action
            if snapshot:
                summary['agent_config'] = snapshot
            record = dict(id=uuid.uuid4().hex, project_id=parent['project_id'], status='QUEUED',
                          backend=parent['backend'], requirements=message, template='chat', max_retries=0,
                          created_at=now, updated_at=now, generation=0, tasks=[], summary=summary)
            db.execute(runs.insert().values(**record))
            self.event(db, record['id'], 'chat_queued', {"parent_run_id": parent_id})
            return {"id": record['id'], "parent_run_id": parent_id, "status": 'QUEUED'}

    def get_events(self, run_id, after=0):
        with self.transaction() as db:
            return [dict(row) for row in db.execute(select(events).where(events.c.run_id == run_id, events.c.id > after)
                    .order_by(events.c.id).limit(200)).mappings()]

    def heartbeat(self, worker_id, backends):
        with self.transaction() as db:
            if db.scalar(select(workers.c.id).where(workers.c.id == worker_id)):
                db.execute(workers.update().where(workers.c.id == worker_id).values(last_seen=time.time(), backends=backends))
            else:
                db.execute(workers.insert().values(id=worker_id, last_seen=time.time(), backends=backends))
            db.execute(runs.update().where(runs.c.worker_id == worker_id, runs.c.status == "RUNNING",
                       runs.c.lease_until > time.time()).values(lease_until=time.time() + LEASE_SECONDS))

    def worker_status(self):
        with self.transaction() as db:
            return [dict(row) for row in db.execute(select(workers).where(workers.c.last_seen > time.time() - 30)).mappings()]

    def claim(self, worker_id):
        with self.transaction() as db:
            self.expire(db)
            worker = db.execute(select(workers).where(workers.c.id == worker_id, workers.c.last_seen > time.time() - 30).with_for_update()).mappings().first()
            if not worker:
                raise StoreConflict("worker must heartbeat before claiming")
            owned = db.execute(select(runs).where(runs.c.worker_id == worker_id, runs.c.status == "RUNNING").limit(1)).mappings().first()
            if owned:
                # A claim response may be lost during API restart; return the same
                # lease to this worker rather than running a second queued job.
                return dict(owned)
            row = db.execute(select(runs).where(runs.c.status == "QUEUED", runs.c.backend.in_(worker["backends"]))
                             .order_by(runs.c.created_at).with_for_update(skip_locked=True).limit(1)).mappings().first()
            if row is None:
                return None
            record = dict(row)
            record.update(status="RUNNING", worker_id=worker_id, generation=record["generation"] + 1,
                          lease_until=time.time() + LEASE_SECONDS, updated_at=time.time())
            db.execute(runs.update().where(runs.c.id == record["id"]).values(**record))
            self.event(db, record["id"], "run_started", {"status": "RUNNING", "generation": record["generation"]})
            return record

    def owned(self, db, run_id, worker_id, generation):
        row = db.execute(select(runs).where(runs.c.id == run_id).with_for_update()).mappings().first()
        if not row:
            raise KeyError(run_id)
        if row["status"] != "RUNNING" or row["worker_id"] != worker_id or row["generation"] != generation or row["lease_until"] < time.time():
            raise StoreConflict("stale or expired worker lease")
        return row

    def task_event(self, run_id, worker_id, generation, kind, task):
        with self.transaction() as db:
            row = self.owned(db, run_id, worker_id, generation)
            if any(item == task for item in row["tasks"]):
                return  # Idempotent retransmission after an HTTP disconnect.
            current = [item for item in row["tasks"] if item["id"] != task["id"]]
            current.append(task)
            db.execute(runs.update().where(runs.c.id == run_id).values(tasks=current, updated_at=time.time()))
            self.event(db, run_id, kind, {key: task.get(key) for key in ("id", "name", "capability", "status", "error")})

    def finish(self, run_id, worker_id, generation, summary):
        # Code is already stored with its task; keep list-query summaries compact.
        summary = {key: value for key, value in summary.items() if key != "final_artifacts"}
        with self.transaction() as db:
            row = db.execute(select(runs).where(runs.c.id == run_id).with_for_update()).mappings().first()
            if row:
                summary = {**{key: row['summary'][key] for key in ('agent_config', 'parent_run_id', 'resume_from_task', 'workspace_mode')
                              if key in row['summary']}, **summary}
            if row and row["status"] in ("COMPLETED", "FAILED") and row["worker_id"] == worker_id and row["generation"] == generation and row["summary"] == summary:
                return
            self.owned(db, run_id, worker_id, generation)
            status = "COMPLETED" if summary.get("status") == "PASS" or (row['template'] == 'prd' and summary.get('status') == 'PRD_READY') or (row['template'] == 'chat' and summary.get('status') == 'CHAT_READY') else "FAILED"
            db.execute(runs.update().where(runs.c.id == run_id).values(status=status, summary=summary,
                       updated_at=time.time(), lease_until=None))
            self.event(db, run_id, "run_finished", {"status": status, "error": summary.get("error")})
