"""Conversation-rooted work items and explicit human delivery acceptance."""

import json
import time
from collections import defaultdict
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from .requirements import requirements as requirement_rows
from .run_store import StoreConflict, events, projects, runs, work_item_acceptance


class AcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    run_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    criteria_confirmed: list[str] = Field(default_factory=list, max_length=100)


class ReworkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instruction: str = Field(min_length=1, max_length=4000, pattern=r"\S")


class CreateWorkItemRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    requirement_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    kind: Literal["FEATURE", "CHANGE", "DEBUG"]
    title: str = Field(min_length=1, max_length=256, pattern=r"\S")
    description: str = Field(min_length=1, max_length=12000, pattern=r"\S")
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)
    backend: Literal["codex", "llm", "mixed", "configured"] = "configured"
    workspace_mode: Literal["isolated", "repository"] = "repository"
    max_retries: int = Field(default=3, ge=0, le=3)


def _title(text):
    try:
        document = json.loads(text)
        if isinstance(document, dict) and isinstance(document.get("title"), str):
            return document["title"].strip()[:160] or "未命名开发任务"
    except (ValueError, TypeError):
        pass
    return (text.strip().splitlines() or ["未命名开发任务"])[0][:160]


def _snapshot(db):
    columns = [runs.c.id, runs.c.project_id, runs.c.status, runs.c.backend,
               runs.c.requirements, runs.c.template, runs.c.max_retries,
               runs.c.created_at, runs.c.updated_at, runs.c.summary]
    rows = [dict(row) for row in db.execute(select(*columns).where(
        runs.c.template.notin_(("chat", "prd"))).order_by(runs.c.created_at).limit(2000)).mappings()]
    by_id = {row["id"]: row for row in rows}

    def root_id(run_id):
        seen = set()
        while run_id in by_id and run_id not in seen:
            seen.add(run_id)
            row = by_id[run_id]
            parent = row["summary"].get("work_item_id") or (
                row["summary"].get("parent_run_id") if row["template"] == "resume" else None)
            if not parent or parent not in by_id:
                break
            run_id = parent
        return run_id

    grouped = defaultdict(list)
    for row in rows:
        grouped[root_id(row["id"])].append(row)
    project_names = {row.id: row.payload.get("name", row.id) for row in db.execute(
        select(projects.c.id, projects.c.payload))}
    requirement_map = {}
    requirements_by_id = {}
    for payload, in db.execute(select(requirement_rows.c.payload)):
        requirements_by_id[payload["id"]] = payload
        if payload.get("run_id"):
            requirement_map[payload["run_id"]] = payload
    acceptance = {row.run_id: row for row in db.execute(select(work_item_acceptance)).mappings()}
    return grouped, project_names, requirement_map, requirements_by_id, acceptance


def _item(root_id, members, project_names, requirement_map, requirements_by_id, acceptance):
    root = next((row for row in members if row["id"] == root_id), members[0])
    latest = max(members, key=lambda row: (row["created_at"], row["id"]))
    decision = acceptance.get(root_id)
    req_id = root["summary"].get("requirement_id") or (
        requirement_map[root_id]["id"] if root_id in requirement_map else None)
    requirement = requirements_by_id.get(req_id)
    document = requirement.get("document") if requirement else None
    try:
        work_document = json.loads(root["requirements"])
    except (ValueError, TypeError):
        work_document = None
    criteria = root["summary"].get("work_item_criteria")
    if criteria is None:
        criteria = work_document.get("acceptance_criteria", []) if isinstance(work_document, dict) else []
    if not isinstance(criteria, list):
        criteria = []
    tested = latest["status"] == "COMPLETED" and (
        latest["summary"].get("final_test_summary") or {}).get("outcome") == "PASSED"
    if latest["status"] in ("QUEUED", "RUNNING"):
        status = "IN_PROGRESS"
    elif tested and decision and decision.accepted_run_id == latest["id"]:
        status = "DELIVERED"
    elif tested:
        status = "AWAITING_ACCEPTANCE"
    else:
        status = "NEEDS_ATTENTION"
    return {
        "id": root_id, "project_id": root["project_id"],
        "project_name": project_names.get(root["project_id"], root["project_id"]),
        "requirement_id": req_id, "requirement_title": (document or {}).get("title") if isinstance(document, dict) else None,
        "title": root["summary"].get("work_item_title") or _title(root["requirements"]),
        "description": root["summary"].get("work_item_description") or root["requirements"],
        "kind": root["summary"].get("work_item_kind") or "TASK",
        "acceptance_criteria": criteria, "status": status,
        "latest_run_id": latest["id"], "latest_run_status": latest["status"],
        "latest_test_summary": latest["summary"].get("final_test_summary"),
        "backend": root["backend"], "created_at": root["created_at"],
        "updated_at": max(row["updated_at"] for row in members),
        "revision": decision.revision if decision else 0,
        "accepted_run_id": decision.accepted_run_id if decision else None,
        "accepted_at": decision.accepted_at if decision else None,
        "run_ids": [row["id"] for row in members],
    }


def _handoff(task):
    if not task:
        return "阶段结果已记录。"
    if task.get("error"):
        return f"执行失败：{str(task['error'])[:500]}"
    output = task.get("output") or {}
    role = task.get("capability")
    if role == "architecture":
        return f"架构契约已提交，包含 {len(output.get('subtasks') or [])} 个子任务。"
    if role == "coding":
        files = list((output.get("files") or {}).keys()) + list((output.get("tests") or {}).keys())
        return "代码与测试已提交：" + ("、".join(files[:8]) or "等待产物")
    if role == "review":
        return f"审查结论：{output.get('review_status', '已记录')}；问题 {len(output.get('issues') or [])} 项。"
    if role == "testing":
        summary = output.get("test_summary") or {}
        return f"沙箱测试：{(summary.get('counts') or {}).get('passed', 0)}/{summary.get('total', 0)} 通过。"
    return "阶段结果已记录。"


def _timeline(db, item, members):
    entries = [{"id": "request-" + item["id"], "time": item["created_at"],
                "speaker": "你", "type": "request", "text": item["description"], "run_id": item["id"]}]
    member_ids = [row["id"] for row in members]
    task_map = {row.id: {task["id"]: task for task in row.tasks or []}
                for row in db.execute(select(runs.c.id, runs.c.tasks).where(runs.c.id.in_(member_ids)))}
    speaker_names = {"architecture": "Architect", "coding": "Coder",
                     "review": "Reviewer", "testing": "Tester"}
    for event in db.execute(select(events).where(events.c.run_id.in_(member_ids))
                            .order_by(events.c.id).limit(1000)).mappings():
        if event.kind == "task_started":
            role = speaker_names.get(event.data.get("capability"), "Agent")
            text = f"开始处理：{event.data.get('name') or role}。"
        elif event.kind == "task_finished":
            role = speaker_names.get(event.data.get("capability"), "Agent")
            text = _handoff(task_map.get(event.run_id, {}).get(event.data.get("id")))
        elif event.kind == "run_finished":
            role = "系统"
            text = "本轮执行已完成，等待你确认交付。" if event.data.get("status") == "COMPLETED" else "本轮执行需要处理，请查看失败证据。"
        elif event.kind == "run_queued" and event.run_id != item["id"]:
            role = "系统"
            text = "已按修改要求启动新一轮开发。"
        else:
            continue
        entries.append({"id": f"event-{event.id}", "time": event.created_at,
                        "speaker": role, "type": event.kind, "text": text, "run_id": event.run_id})
    chats = db.execute(select(runs.c.id, runs.c.requirements, runs.c.status, runs.c.created_at,
                              runs.c.updated_at, runs.c.summary).where(
        runs.c.template == "chat", runs.c.summary["parent_run_id"].as_string().in_(member_ids))
        .order_by(runs.c.created_at).limit(500)).mappings()
    for chat in chats:
        entries.append({"id": "user-" + chat.id, "time": chat.created_at,
                        "speaker": "你", "type": "message", "text": chat.requirements,
                        "run_id": chat.summary.get("parent_run_id")})
        answer = chat.summary.get("answer") or (chat.summary.get("error") if chat.status == "FAILED" else None)
        if answer:
            entries.append({"id": "lead-" + chat.id, "time": chat.updated_at,
                            "speaker": "Team Lead", "type": "reply", "text": str(answer),
                            "run_id": chat.summary.get("parent_run_id")})
    if item["accepted_at"] and item["accepted_run_id"]:
        entries.append({"id": "accepted-" + item["accepted_run_id"], "time": item["accepted_at"],
                        "speaker": "你", "type": "acceptance", "text": "已确认本轮交付。",
                        "run_id": item["accepted_run_id"]})
    return sorted(entries, key=lambda entry: (entry["time"], entry["id"]))


def install_work_items(app):
    def store():
        return app.state.run_store

    def find(db, item_id):
        grouped, names, reqs, req_by_id, accepted = _snapshot(db)
        members = grouped.get(item_id)
        if not members or all(row["id"] != item_id for row in members):
            raise HTTPException(404, "work item not found")
        return _item(item_id, members, names, reqs, req_by_id, accepted), members

    @app.post("/v1/work-items", status_code=202)
    def create(payload: CreateWorkItemRequest):
        with store().transaction() as db:
            requirement = db.scalar(select(requirement_rows.c.payload).where(
                requirement_rows.c.id == payload.requirement_id,
                requirement_rows.c.project_id == payload.project_id))
            if requirement is None:
                raise HTTPException(404, "requirement not found in project")
            document = requirement.get("document") or {}
        criteria = [text.strip() for text in payload.acceptance_criteria]
        if any(not text or len(text) > 512 for text in criteria) or len(set(criteria)) != len(criteria):
            raise HTTPException(422, "验收条件需为不重复的 1-512 字文本")
        title = payload.title.strip()
        description = payload.description.strip()
        context = f"所属需求：{document.get('title') or payload.requirement_id}\n业务目标：{document.get('business_context') or '未填写'}"
        text = f"工作项：{title}\n{description}\n\n{context}"
        if criteria:
            text += "\n\n验收条件：\n" + "\n".join(f"- {criterion}" for criterion in criteria)
        if len(text) > 16000:
            raise HTTPException(422, "工作项上下文超过 16000 字")
        try:
            run = store().create_run(project_id=payload.project_id, backend=payload.backend,
                requirements=text, template="custom", max_retries=payload.max_retries,
                workspace_mode=payload.workspace_mode, requirement_id=payload.requirement_id,
                work_item_kind=payload.kind, work_item_title=title,
                work_item_description=description, work_item_criteria=criteria)
        except KeyError:
            raise HTTPException(404, "project not found") from None
        except StoreConflict as exc:
            raise HTTPException(409, str(exc)) from None
        return {"work_item_id": run["id"], "run_id": run["id"], "status": run["status"]}

    @app.get("/v1/work-items")
    def listing(project_id: str | None = None, requirement_id: str | None = None):
        with store().transaction() as db:
            grouped, names, reqs, req_by_id, accepted = _snapshot(db)
            items = [_item(root, members, names, reqs, req_by_id, accepted) for root, members in grouped.items()]
        items = [item for item in items if (not project_id or item["project_id"] == project_id)
                 and (not requirement_id or item["requirement_id"] == requirement_id)]
        compact = [{key: value for key, value in item.items() if key not in
                    ("description", "acceptance_criteria", "latest_test_summary")}
                   for item in items]
        return {"work_items": sorted(compact, key=lambda item: item["created_at"], reverse=True)}

    @app.get("/v1/work-items/{item_id}")
    def detail(item_id: str):
        with store().transaction() as db:
            item, members = find(db, item_id)
            item["runs"] = [{"id": row["id"], "status": row["status"], "backend": row["backend"],
                             "created_at": row["created_at"], "summary": {
                                 key: row["summary"].get(key) for key in ("error", "final_test_summary", "delivery")}}
                            for row in sorted(members, key=lambda row: row["created_at"], reverse=True)]
            item["timeline"] = _timeline(db, item, members)
            return item

    @app.post("/v1/work-items/{item_id}/accept")
    def accept(item_id: str, payload: AcceptRequest):
        with store().transaction() as db:
            root = db.scalar(select(runs.c.id).where(runs.c.id == item_id).with_for_update())
            if not root:
                raise HTTPException(404, "work item not found")
            item, _ = find(db, item_id)
            if item["revision"] != payload.revision:
                raise HTTPException(409, "交付状态已更新，请刷新后重试")
            if item["latest_run_id"] != payload.run_id or item["status"] != "AWAITING_ACCEPTANCE":
                raise HTTPException(409, "只有最新且已通过沙箱测试的运行可以确认交付")
            if set(payload.criteria_confirmed) != set(item["acceptance_criteria"]):
                raise HTTPException(422, "请逐项确认当前需求的验收条件")
            values = {"revision": payload.revision + 1, "accepted_run_id": payload.run_id,
                      "accepted_at": time.time()}
            existing = db.scalar(select(work_item_acceptance.c.run_id).where(work_item_acceptance.c.run_id == item_id)
                                 .with_for_update())
            if existing:
                db.execute(work_item_acceptance.update().where(work_item_acceptance.c.run_id == item_id).values(**values))
            else:
                db.execute(work_item_acceptance.insert().values(run_id=item_id, **values))
        return detail(item_id)

    @app.post("/v1/work-items/{item_id}/runs", status_code=202)
    def rework(item_id: str, payload: ReworkRequest):
        with store().transaction() as db:
            item, members = find(db, item_id)
            if item["latest_run_status"] in ("QUEUED", "RUNNING"):
                raise HTTPException(409, "当前工作项已有正在执行的运行")
            root = next(row for row in members if row["id"] == item_id)
            original = root["requirements"]
            instruction = payload.instruction.strip()
            text = f"{original}\n\n本轮修改要求：\n{instruction}"
            if len(text) > 16000:
                raise HTTPException(422, "需求与修改要求合计超过 16000 字")
            kwargs = dict(project_id=item["project_id"], backend=root["backend"], requirements=text,
                          template="custom", max_retries=root["max_retries"],
                          workspace_mode=root["summary"].get("workspace_mode", "isolated"),
                          work_item_id=item_id, requirement_id=item["requirement_id"],
                          work_item_note=instruction)
        try:
            return {"work_item_id": item_id, "run": store().create_run(**kwargs)}
        except StoreConflict as exc:
            raise HTTPException(409, str(exc)) from None
