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

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from rd_platform.domain import Project, Task, TaskInput, TaskOutput
from rd_platform.agent_management import validate_agent_config
from rd_platform.manager import AgentRegistry, DispatchConflict, Dispatcher, create_default_registry
from .run_store import RunStore, StoreConflict
from .workbench import install_workbench, worker_token
from .requirements import install_requirements

MAX_BYTES = 1024 * 1024
STATIC_DIR = Path(__file__).resolve().parent / "static"
AGENTS_PAGE = STATIC_DIR / "agents.html"


@dataclass(frozen=True)
class Settings:
    data: Path
    logs: Path
    models: Path
    database_url: str | None = None

    @classmethod
    def from_env(cls):
        return cls(*(Path(os.environ.get(name, default)) for name, default in (
            ("RD_DATA_DIR", "/app/data"), ("RD_LOG_DIR", "/app/logs"), ("RD_MODELS_DIR", "/app/models"),
        )), database_url=os.environ.get("RD_DATABASE_URL"))


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


class WorkspaceMilestone(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=128)
    goal: str = Field(default="", max_length=4000)
    packages: list[Annotated[str, Field(min_length=1, max_length=512)]] = Field(max_length=100)
    run_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class WorkspaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = Field(ge=0)
    milestones: list[WorkspaceMilestone] = Field(max_length=50)
    accepted: list[Annotated[str, Field(max_length=4096)]] = Field(default_factory=list, max_length=1000)


class AgentConfigRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    revision: StrictInt = Field(ge=0)
    agents: dict


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
            limit = 8 * MAX_BYTES if scope.get("path", "").startswith("/internal/worker/") else MAX_BYTES
            if len(body) > limit:
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
        # PostgreSQL in Compose; SQLite is only for isolated single-process tests.
        database = RunStore(settings.database_url or f"sqlite:///{settings.data / 'workbench-test.db'}")
        app.state.run_store = database
        app.state.worker_token = worker_token(settings.data)
        app.state.ready = True
        logger.info("service started")
        try:
            yield
        finally:
            app.state.ready = False
            logger.info("service stopped")
            logger.removeHandler(handler)
            handler.close()
            database.close()

    app = FastAPI(title="R&D Platform Development API", version="0.3.0", lifespan=lifespan)
    app.add_middleware(BodyLimit)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["localhost", "127.0.0.1", "[::1]", "api", "testserver"])
    app.state.agent_registry = registry
    app.state.dispatcher = dispatcher
    app.state.projects = projects

    @app.middleware("http")
    async def browser_boundary(request: Request, call_next):
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin and origin != str(request.base_url).rstrip("/"):
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "cross-origin mutation is not allowed"}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path == "/" or request.url.path == "/agents" or request.url.path.startswith("/assets/"):
            response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'"
            response.headers["Cache-Control"] = "no-store"
        elif request.url.path.startswith("/v1/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    install_workbench(app)
    install_requirements(app)

    @app.get("/agents", include_in_schema=False)
    async def agents_page():
        return FileResponse(AGENTS_PAGE, media_type="text/html")

    @app.get("/v1/projects")
    async def list_projects():
        return {"projects": app.state.run_store.list_projects()}

    @app.get("/v1/projects/{id}")
    async def get_project(id: str):
        record = app.state.run_store.get_project(id)
        if record is None:
            raise HTTPException(404, "project not found")
        return record

    @app.get("/v1/agents")
    async def list_agents():
        return {"agents": [metadata.to_dict() for metadata in registry.list_agents()]}

    @app.get("/v1/projects/{id}/agents/config")
    async def get_agent_config(id: str):
        try:
            return app.state.run_store.get_agent_config(id)
        except KeyError:
            raise HTTPException(404, "project not found") from None

    @app.put("/v1/projects/{id}/agents/config")
    async def put_agent_config(id: str, request: AgentConfigRequest):
        try:
            agents = validate_agent_config({"agents": request.agents})["agents"]
            if agents["TESTER"]["backend"] != "sandbox":
                raise ValueError("TESTER 必须使用 Docker 沙箱执行")
            return app.state.run_store.update_agent_config(id, request.revision, agents)
        except ValueError as error:
            if isinstance(error, StoreConflict):
                raise HTTPException(409, str(error)) from None
            raise HTTPException(422, str(error)) from None
        except KeyError:
            raise HTTPException(404, "project not found") from None

    @app.put("/v1/projects/{id}/workspace")
    async def update_workspace(id: str, request: WorkspaceRequest):
        try:
            return app.state.run_store.update_workspace(id, request.model_dump())
        except KeyError:
            raise HTTPException(404, "project not found") from None
        except StoreConflict as error:
            raise HTTPException(409, str(error)) from None

    @app.post("/v1/projects", status_code=201)
    async def create_project(request: ProjectRequest):
        # Bounded, single-process development store; no persistence claim.
        if "agent_config" in request.context:
            raise HTTPException(422, "Agent 配置须通过专用接口保存")
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
        try:
            app.state.run_store.save_project(project.to_dict())
        except StoreConflict as exc:
            raise HTTPException(409, str(exc)) from None
        projects[project.id] = project
        logger.info("created project=%s tasks=%s", project.id, len(project.tasks))
        return project.to_dict()

    @app.post("/v1/projects/{id}/dispatch")
    async def dispatch_project(id: str):
        project = projects.get(id)
        if project is None:
            record = app.state.run_store.get_project(id)
            if record is None:
                raise HTTPException(404, "project not found")
            project = Project(record["name"], id=record["id"], context=record["context"])
            for item in record["tasks"]:
                options = {**item, "input": TaskInput(item["input"]),
                           "output": TaskOutput(item["output"]) if item["output"] is not None else None}
                if options["status"] == "RUNNING":
                    options.update(status="FAILED", error="service restarted during legacy dispatch")
                project.add_task(Task(**options))
            projects[id] = project
        try:
            await dispatcher.dispatch(project)
        except DispatchConflict as exc:
            raise HTTPException(409, str(exc)) from None
        logger.info("dispatched project=%s states=%s", id, [task.status.value for task in project.tasks])
        app.state.run_store.save_project(project.to_dict())
        return project.to_dict()

    @app.get("/health")
    async def health():
        if not getattr(app.state, "ready", False) or not all(p.is_dir() for p in (settings.data, settings.logs, settings.models)):
            raise HTTPException(503, "storage not ready")
        return {"status": "ok", "service": "api", "version": "0.3.0"}

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
