"""Trusted static algorithms and bounded JSON storage; no dynamic-code API."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Annotated

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from rd_platform.domain import Project, Task, TaskInput
from rd_platform.manager import AgentRegistry, DispatchConflict, Dispatcher, create_default_registry

MAX_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Settings:
    data: Path
    logs: Path
    models: Path

    @classmethod
    def from_env(cls):
        return cls(*(Path(os.environ.get(name, default)) for name, default in (
            ("RD_DATA_DIR", "/app/data"), ("RD_LOG_DIR", "/app/logs"), ("RD_MODELS_DIR", "/app/models"),
        )))


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=128)
    score: StrictInt = Field(ge=-(2**63), le=2**63 - 1)
    active: StrictBool = True


class Records(BaseModel):
    model_config = ConfigDict(extra="forbid")
    records: list[Record] = Field(max_length=1000)


class FilterRequest(Records):
    min_score: StrictInt = 0
    limit: Annotated[StrictInt, Field(ge=0, le=1000)] | None = None


class TaskRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str | None = Field(default=None, min_length=1, max_length=128)
    name: str = Field(min_length=1, max_length=256)
    capability: str = Field(min_length=1, max_length=128)
    input: dict = Field(default_factory=dict)
    depends_on: list[Annotated[str, Field(min_length=1, max_length=128)]] = Field(default_factory=list, max_length=100)


class ProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=256)
    context: dict = Field(default_factory=dict)
    tasks: list[TaskRequest] = Field(default_factory=list, max_length=100)


class BodyLimit:
    """Bound actual streamed bytes, including requests without Content-Length."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > MAX_BYTES:
                await send({"type": "http.response.start", "status": 413, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"detail":"request exceeds 1 MiB"}'})
                return
            if not message.get("more_body", False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def create_app(settings=None, *, registry: AgentRegistry | None = None):
    settings = settings or Settings.from_env()
    logger = logging.getLogger(f"rd-platform.api.{id(settings)}")
    if registry is None:
        registry = create_default_registry()
    dispatcher = Dispatcher(registry)
    projects: dict[str, Project] = {}

    @asynccontextmanager
    async def lifespan(app):
        # Fail startup if mounts are missing/unwritable instead of claiming readiness.
        for path in (settings.data, settings.logs, settings.models):
            if not path.is_dir():
                raise RuntimeError(f"required mount missing: {path}")
        with tempfile.TemporaryFile(dir=settings.data) as check:
            check.write(b"ready")
            check.flush()
        handler = RotatingFileHandler(settings.logs / "service.log", maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        app.state.ready = True
        logger.info("service started")
        try:
            yield
        finally:
            app.state.ready = False
            logger.info("service stopped")
            logger.removeHandler(handler)
            handler.close()

    app = FastAPI(title="R&D Platform Development API", version="0.2.0", lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.state.agent_registry = registry
    app.state.dispatcher = dispatcher
    app.state.projects = projects

    @app.get("/v1/agents")
    async def list_agents():
        return {"agents": [metadata.to_dict() for metadata in registry.list_agents()]}

    @app.post("/v1/projects", status_code=201)
    async def create_project(request: ProjectRequest):
        # Bounded, single-process development store; no persistence claim.
        if len(projects) >= 100:
            raise HTTPException(409, "in-memory project capacity reached (100); restart to clear")
        try:
            project = Project(name=request.name, context=request.context)
            for item in request.tasks:
                options = {"id": item.id} if item.id is not None else {}
                project.add_task(Task(name=item.name, capability=item.capability, input=TaskInput(item.input),
                                      depends_on=item.depends_on, **options))
            project.build_dag()
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        projects[project.id] = project
        logger.info("created project=%s tasks=%s", project.id, len(project.tasks))
        return project.to_dict()

    @app.post("/v1/projects/{id}/dispatch")
    async def dispatch_project(id: str):
        project = projects.get(id)
        if project is None:
            raise HTTPException(404, "project not found")
        try:
            await dispatcher.dispatch(project)
        except DispatchConflict as exc:
            raise HTTPException(409, str(exc)) from None
        logger.info("dispatched project=%s states=%s", id, [task.status.value for task in project.tasks])
        return project.to_dict()

    @app.get("/health")
    async def health():
        if not getattr(app.state, "ready", False) or not all(p.is_dir() for p in (settings.data, settings.logs, settings.models)):
            raise HTTPException(503, "storage not ready")
        return {"status": "ok", "service": "api", "version": "0.2.0"}

    @app.post("/v1/algorithms/filter")
    async def filter_records(request: FilterRequest):
        selected = {}
        for record in request.records:
            if not record.active or record.score < request.min_score:
                continue
            if record.id not in selected or record.score > selected[record.id]["score"]:
                selected[record.id] = {"id": record.id, "score": record.score}
        records = sorted(selected.values(), key=lambda item: (-item["score"], item["id"]))
        if request.limit is not None:
            records = records[:request.limit]
        logger.info("filter input=%s output=%s", len(request.records), len(records))
        return {"records": records, "count": len(records)}

    def dataset_path(name):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
            raise HTTPException(422, "dataset name must contain only letters, digits, _ or -")
        return settings.data / f"{name}.json"

    @app.put("/v1/data/{name}")
    async def put_dataset(name: str, payload: Records):
        path = dataset_path(name)
        content = json.dumps(payload.model_dump(), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(content) > MAX_BYTES:
            raise HTTPException(413, "dataset exceeds 1 MiB")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=settings.data, prefix=".dataset-", delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        logger.info("stored dataset=%s records=%s", name, len(payload.records))
        return {"dataset": name, "count": len(payload.records), "sha256": hashlib.sha256(content).hexdigest()}

    @app.get("/v1/data/{name}")
    async def get_dataset(name: str):
        path = dataset_path(name)
        try:
            # Refuse symlinks and non-regular files; no paths supplied by clients.
            descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_BYTES:
                    raise HTTPException(422, "invalid dataset file")
                raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    raise HTTPException(413, "dataset exceeds 1 MiB")
            payload = Records.model_validate_json(raw)
        except FileNotFoundError:
            raise HTTPException(404, "dataset not found") from None
        except (OSError, ValueError):
            raise HTTPException(422, "invalid dataset file") from None
        return {"dataset": name, "records": payload.model_dump()["records"], "count": len(payload.records)}

    @app.get("/v1/models")
    async def models():
        names = sorted(path.name for path in settings.models.iterdir() if path.is_file() and not path.is_symlink())[:100]
        return {"models": names, "read_only": True}

    return app


app = create_app()
