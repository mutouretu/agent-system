"""Run only inside Docker; service extras are optional for runtime-only tests."""

import hashlib
import json

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient
from rd_platform.service.api import MAX_BYTES, Settings, create_app
from rd_platform.service import probe_api


@pytest.fixture
def service(tmp_path):
    settings = Settings(*(tmp_path / name for name in ("data", "logs", "models")))
    for path in (settings.data, settings.logs, settings.models):
        path.mkdir()
    with TestClient(create_app(settings)) as client:
        yield client, settings


def test_health_and_storage(service):
    client, settings = service
    assert client.get("/health").json()["status"] == "ok"
    payload = {"records": [{"id": "中文", "score": 3, "active": True}]}
    saved = client.put("/v1/data/sample", json=payload)
    assert saved.status_code == 200
    assert saved.json()["sha256"] == hashlib.sha256((settings.data / "sample.json").read_bytes()).hexdigest()
    assert client.get("/v1/data/sample").json()["records"] == payload["records"]
    assert client.get("/v1/data/missing").status_code == 404
    assert (settings.logs / "service.log").stat().st_size > 0


def test_filter_boundaries(service):
    client, _ = service
    payload = {"records": [
        {"id": "b", "score": 5}, {"id": "a", "score": 5},
        {"id": "a", "score": 4}, {"id": "c", "score": 6, "active": False},
    ], "min_score": 5}
    assert client.post("/v1/algorithms/filter", json=payload).json() == {
        "records": [{"id": "a", "score": 5}, {"id": "b", "score": 5}], "count": 2,
    }
    payload["limit"] = 0
    assert client.post("/v1/algorithms/filter", json=payload).json()["records"] == []


@pytest.mark.parametrize("payload", [
    {"records": [{"id": "a", "score": True}]},
    {"records": [{"id": "a", "score": "2"}]},
    {"records": [], "limit": -1}, {"records": [], "min_score": False},
    {"records": [{"id": "a", "score": 2, "active": "true"}]},
])
def test_strict_input(service, payload):
    assert service[0].post("/v1/algorithms/filter", json=payload).status_code == 422


def test_reject_paths_symlinks_and_corrupt_data(service, tmp_path):
    client, settings = service
    assert client.put("/v1/data/bad.name", json={"records": []}).status_code == 422
    outside = tmp_path / "outside.json"
    outside.write_text('{"records":[]}')
    (settings.data / "link.json").symlink_to(outside)
    assert client.get("/v1/data/link").status_code == 422
    # Replacing a symlink replaces its directory entry, never the target.
    assert client.put("/v1/data/link", json={"records": [{"id": "a", "score": 2}]}).status_code == 200
    assert outside.read_text() == '{"records":[]}'
    (settings.data / "broken.json").write_text("invalid")
    assert client.get("/v1/data/broken").status_code == 422


def test_body_limit(service):
    assert service[0].put("/v1/data/big", content=b"x" * (MAX_BYTES + 1)).status_code == 413


def test_models_and_readiness(service):
    client, settings = service
    (settings.models / "weight.bin").write_bytes(b"sample")
    (settings.models / "link").symlink_to(settings.models / "weight.bin")
    assert client.get("/v1/models").json() == {"models": ["weight.bin"], "read_only": True}
    settings.data.rmdir()
    assert client.get("/health").status_code == 503


def test_missing_mount_fails_startup(tmp_path):
    with pytest.raises(RuntimeError, match="required mount missing"):
        with TestClient(create_app(Settings(tmp_path / "missing", tmp_path, tmp_path))):
            pass


def test_probe_real_report():
    probe_api._cached = None
    with TestClient(probe_api.app) as client:
        assert client.get("/health").json()["runtime_probe"] == "ok"
        report = client.get("/probe").json()
        assert report["status"] == "ok" and report["result"] == 16
        assert len(json.dumps(report).encode()) < 64 * 1024
        assert report["samples"]


def test_probe_failure_is_unhealthy(monkeypatch):
    monkeypatch.setattr(probe_api, "snapshot", lambda: {"status": "error", "result": None})
    with TestClient(probe_api.app) as client:
        assert client.get("/health").status_code == 503
