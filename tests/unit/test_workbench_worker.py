from types import SimpleNamespace
import httpx

import pytest

from scripts import workbench_worker as module


def test_worker_preflight_closes_sdk_client_without_context_manager(tmp_path, monkeypatch):
    wheel = tmp_path / "sandbox-validation/wheels/pytest-8.3.5-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    wheel.touch()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    calls = []
    client = SimpleNamespace(ping=lambda: calls.append("ping"), close=lambda: calls.append("close"),
                             images=SimpleNamespace(get=lambda image: calls.append(image)))
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    monkeypatch.setattr(module, "CodexArtifactGenerator", lambda path: None)
    def missing():
        raise module.LLMConfigurationError("missing")
    monkeypatch.setattr(module, "create_provider", missing)
    worker = object.__new__(module.Worker)
    worker.directory = tmp_path
    worker.backends = []
    worker.post = lambda path, **kwargs: calls.append((path, kwargs))
    worker.preflight()
    assert calls[:3] == ["ping", "python:3.12-slim", "close"]
    assert worker.backends == ["codex", "configured", "prd_codex"]
    assert calls[-1] == ("/heartbeat", {"backends": ["codex", "configured", "prd_codex"]})


def test_worker_advertises_mixed_only_with_both_backends(tmp_path, monkeypatch):
    wheel = tmp_path / "sandbox-validation/wheels/pytest-8.3.5-py3-none-any.whl"
    wheel.parent.mkdir(parents=True)
    wheel.touch()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    client = SimpleNamespace(ping=lambda: None, close=lambda: None,
                             images=SimpleNamespace(get=lambda image: None))
    monkeypatch.setattr(module.docker, "from_env", lambda **kwargs: client)
    monkeypatch.setattr(module, "CodexArtifactGenerator", lambda path: None)
    monkeypatch.setattr(module, "create_provider", lambda: object())
    worker = object.__new__(module.Worker)
    worker.directory = tmp_path
    worker.backends = []
    worker.post = lambda path, **kwargs: None
    worker.preflight()
    assert worker.backends == ["codex", "llm", "mixed", "configured", "prd_codex", "prd_llm"]


@pytest.mark.parametrize("url", ["https://example.com", "http://remote:8000", "http://user:pass@localhost:8000"])
def test_worker_rejects_nonlocal_api_before_reading_credentials(url):
    with pytest.raises(ValueError, match="localhost"):
        module.Worker(url, "/must/not/be/read")


def test_worker_retries_lost_http_response_without_changing_worker_identity(monkeypatch):
    requests = []
    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            raise httpx.RemoteProtocolError("restarting API")
        return httpx.Response(200, json={"run": None})
    worker = object.__new__(module.Worker)
    worker.id = "a" * 32
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    with httpx.Client(base_url="http://localhost", transport=httpx.MockTransport(handler)) as client:
        worker.client = client
        assert worker.post("/claim") == {"run": None}
    assert len(requests) == 2 and requests[0].content == requests[1].content
