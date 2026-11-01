"""Browser routes and authenticated local-worker API. No execution in this process."""

import asyncio
import hmac
import json
from pathlib import Path
import secrets
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from .run_store import StoreConflict, TERMINAL


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class RunRequest(StrictModel):
    project_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    requirements: str = Field(min_length=1, max_length=16000, pattern=r"\S")
    backend: Literal["codex", "llm"] = "codex"
    template: Literal["custom", "lru"] = "custom"
    max_retries: StrictInt = Field(default=3, ge=0, le=3)


class WorkerRequest(StrictModel):
    worker_id: str = Field(pattern=r"^[a-f0-9]{32}$")


class Heartbeat(WorkerRequest):
    backends: list[Literal["codex", "llm", "prd_codex", "prd_llm"]] = Field(max_length=4)


class Lease(WorkerRequest):
    generation: StrictInt = Field(ge=1)


class TaskEvent(Lease):
    kind: Literal["task_started", "task_finished"]
    task: dict


class Finish(Lease):
    summary: dict


def install_workbench(app):
    def store(request: Request):
        return request.app.state.run_store

    def authorize(request: Request):
        token = request.headers.get("X-Worker-Token", "")
        if not hmac.compare_digest(token, request.app.state.worker_token):
            raise HTTPException(401, "worker authentication required")

    def translate(operation, *args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except KeyError:
            raise HTTPException(404, "project or run not found") from None
        except StoreConflict as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/v1/workbench")
    def capabilities(request: Request):
        return {"workers": store(request).worker_status(), "roles": ["ARCHITECT", "CODER", "REVIEWER", "TESTER"],
                "default_backend": "codex", "execution": "docker", "database": store(request).engine.dialect.name}

    @app.post("/v1/runs", status_code=202)
    def submit(payload: RunRequest, request: Request):
        return translate(store(request).create_run, **payload.model_dump())

    @app.get("/v1/runs")
    def list_runs(request: Request, project_id: str | None = None):
        return {"runs": store(request).list_runs(project_id)}

    @app.get("/v1/runs/{run_id}")
    def detail(run_id: str, request: Request):
        return translate(store(request).get_run, run_id)

    @app.get("/v1/runs/{run_id}/artifacts")
    def artifacts(run_id: str, request: Request):
        run = translate(store(request).get_run, run_id)
        result = []
        for task in run["tasks"]:
            output = task.get("output") or {}
            if task.get("capability") == "coding":
                for kind in ("files", "tests"):
                    for path, content in output.get(kind, {}).items():
                        result.append({"task_id": task["id"], "kind": kind, "path": path, "content": content})
            elif output:
                result.append({"task_id": task["id"], "kind": task.get("capability"),
                               "path": task["id"] + ".json", "content": json.dumps(output, ensure_ascii=False, indent=2)})
        return {"artifacts": result}

    @app.get("/v1/runs/{run_id}/events")
    async def stream_events(run_id: str, request: Request, after: int = 0):
        database = store(request)
        await asyncio.to_thread(translate, database.get_run, run_id)
        try:
            cursor = max(0, after, int(request.headers.get("last-event-id", "0")))
        except ValueError:
            raise HTTPException(422, "invalid event cursor") from None

        async def stream():
            nonlocal cursor
            while not await request.is_disconnected():
                batch = await asyncio.to_thread(database.get_events, run_id, cursor)
                for event in batch:
                    cursor = event["id"]
                    yield f"id: {cursor}\nevent: update\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                run = await asyncio.to_thread(database.get_run, run_id)
                if run["status"] in TERMINAL and not batch:
                    if await asyncio.to_thread(database.get_events, run_id, cursor):
                        continue  # Finish may have committed after the previous event read.
                    yield "event: done\ndata: {}\n\n"
                    break
                if not batch:
                    yield ": heartbeat\n\n"
                    await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    internal = APIRouter(prefix="/internal/worker", dependencies=[Depends(authorize)])

    @internal.post("/heartbeat")
    def heartbeat(payload: Heartbeat, request: Request):
        store(request).heartbeat(payload.worker_id, payload.backends)
        return {"ok": True}

    @internal.post("/claim")
    def claim(payload: WorkerRequest, request: Request):
        return {"run": translate(store(request).claim, payload.worker_id)}

    @internal.post("/runs/{run_id}/events")
    def event(run_id: str, payload: TaskEvent, request: Request):
        task = payload.task
        if not isinstance(task.get("id"), str) or not task["id"] or len(task["id"]) > 128:
            raise HTTPException(422, "task id required")
        translate(store(request).task_event, run_id, **payload.model_dump())
        return {"ok": True}

    @internal.post("/runs/{run_id}/finish")
    def finish(run_id: str, payload: Finish, request: Request):
        if payload.summary.get('status') == 'PRD_READY':
            from rd_platform.agents.dev.requirements import RequirementReview
            from pydantic import ValidationError
            run = translate(store(request).get_run, run_id)
            if run['template'] != 'prd':
                raise HTTPException(422, 'PRD result requires a requirement analysis job')
            try:
                RequirementReview.model_validate(payload.summary.get('prd'))
            except ValidationError:
                raise HTTPException(422, 'invalid PRD result') from None
        if payload.summary.get("status") == "PASS":
            if payload.summary.get("execution_mode") != "docker" or payload.summary.get("final_test_summary", {}).get("outcome") != "PASSED":
                raise HTTPException(422, "PASS requires Docker test evidence")
        translate(store(request).finish, run_id, **payload.model_dump())
        return {"ok": True}

    app.include_router(internal)
    static = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static), name="workbench-assets")

    @app.get("/", include_in_schema=False)
    def home():
        return FileResponse(static / "index.html")


def worker_token(data_dir):
    """Application-local worker credential; never reads Codex/system credentials."""
    path = data_dir / ".workbench-worker-token"
    if not path.exists():
        import os
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as stream:
            stream.write(secrets.token_urlsafe(32))
    if path.is_symlink():
        raise RuntimeError("invalid worker token file")
    value = path.read_text().strip()
    if not 32 <= len(value) <= 128:
        raise RuntimeError("invalid worker token")
    return value
