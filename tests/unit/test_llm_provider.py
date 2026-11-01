"""No real networking, model calls, or candidate execution; run this in Docker."""

import asyncio
import json

import httpx
import pytest

from rd_platform.llm import (OpenAIProvider, LLMProvider, LLMResponse, create_provider, LLMArtifactGenerator,
    LLMError, LLMConfigurationError, MissingLLMConfiguration, LLMAuthenticationError,
    LLMRateLimitError, LLMTimeoutError, LLMResponseError)
from rd_platform.agents import AgentInput
from rd_platform.agents.dev import ArchitectAgent, CoderAgent, ReviewerAgent
from rd_platform.agents.dev.interfaces import GenerationRequest
from rd_platform.agents import ARCHITECT
from rd_platform.manager.software_team import SoftwareTeamWorkflow
from tests.unit.test_software_team import ARCHITECTURE, CODE, FakeExecutor


def completion(content='{"ok": true}', **changes):
    result = {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
              "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}}
    result.update(changes)
    return result


def generate(provider):
    return asyncio.run(provider.generate(system_prompt="Design", input_data={"need": "cache"}, output_schema={"type": "object"}))


def provider_with(handler, **kwargs):
    return OpenAIProvider("unit-secret", transport=httpx.MockTransport(handler), **kwargs)


def test_request_url_auth_schema_and_token_usage():
    def handler(request):
        assert str(request.url) == "https://compatible.test/api/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer unit-secret"
        payload = json.loads(request.content)
        assert payload["model"] == "custom-coder" and payload["stream"] is False
        assert payload["response_format"] == {"type": "json_object"}
        assert "JSON" in payload["messages"][0]["content"]
        assert json.loads(payload["messages"][1]["content"])["output_schema"] == {"type": "object"}
        assert "tools" not in payload
        return httpx.Response(200, json=completion())
    result = generate(provider_with(handler, base_url="https://compatible.test/api/v1/", model="custom-coder"))
    assert result.structured_data == {"ok": True}
    assert result.content == '{"ok": true}' and result.token_usage["total_tokens"] == 18


def test_text_mode_local_no_auth_and_missing_usage():
    def handler(request):
        assert "authorization" not in request.headers
        assert "response_format" not in json.loads(request.content)
        return httpx.Response(200, json=completion(usage=None))
    provider = OpenAIProvider(base_url="http://127.0.0.1:8002/v1", model="local", require_api_key=False,
                              response_format="text", transport=httpx.MockTransport(handler))
    assert generate(provider).token_usage == {}


@pytest.mark.parametrize("status,error", [(401, LLMAuthenticationError), (403, LLMAuthenticationError),
    (429, LLMRateLimitError), (400, LLMError), (500, LLMError), (302, LLMError)])
def test_http_failures_are_sanitized_and_not_retried(status, error):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(status, text="leaked unit-secret", headers={"location": "https://elsewhere.test"})
    with pytest.raises(error) as caught:
        generate(provider_with(handler))
    assert "unit-secret" not in str(caught.value) and len(calls) == 1


@pytest.mark.parametrize("error,expected", [(httpx.ReadTimeout, LLMTimeoutError), (httpx.ConnectError, LLMError)])
def test_transport_failure_is_sanitized(error, expected):
    def handler(request):
        raise error("unit-secret", request=request)
    with pytest.raises(expected) as caught:
        generate(provider_with(handler))
    assert "unit-secret" not in str(caught.value)


def test_total_timeout_and_cancellation_propagation():
    async def slow(request):
        await asyncio.sleep(1)
        return httpx.Response(200, json=completion())
    with pytest.raises(LLMTimeoutError):
        generate(provider_with(slow, timeout=0.01))
    async def cancelled(request):
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        generate(provider_with(cancelled))


@pytest.mark.parametrize("content", ["", "```json\n{}\n```", "[]", "not json", '{"a": NaN}',
                                       '{"a": 1e999}', '{"a": 1, "a": 2}'])
def test_invalid_model_json_is_rejected(content):
    with pytest.raises(LLMResponseError):
        generate(provider_with(lambda request: httpx.Response(200, json=completion(content))))


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [None]}, completion(usage={"total_tokens": -1}),
    completion(usage={"total_tokens": True}), completion(usage=[]),
    {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "{}", "refusal": "no"}}]},
    {"choices": [{"finish_reason": "stop", "message": {"content": "{}", "tool_calls": [{}]}}]}])
def test_invalid_envelope_truncation_refusal_and_tools(body):
    with pytest.raises(LLMResponseError):
        generate(provider_with(lambda request: httpx.Response(200, json=body)))


def test_non_json_and_oversized_http_responses():
    for raw in (b"<html>bad gateway</html>", b" " * (2 * 1024 * 1024 + 1)):
        with pytest.raises(LLMResponseError):
            generate(provider_with(lambda request: httpx.Response(200, content=raw)))


@pytest.mark.parametrize("overrides", [{"base_url": "https://user:unit-secret@host/v1"},
    {"base_url": "https://host/v1?key=unit-secret"}, {"base_url": "file:///tmp/model"},
    {"model": " "}, {"timeout": float("inf")}, {"max_tokens": True}, {"response_format": "wrong"}])
def test_bad_config_does_not_echo_values(overrides):
    with pytest.raises(LLMConfigurationError) as caught:
        OpenAIProvider("unit-secret", **overrides)
    assert "unit-secret" not in str(caught.value)


def test_factory_precedence_file_environment_and_overrides(tmp_path):
    config = tmp_path / "llm.json"
    config.write_text(json.dumps({"api_key": "file-key", "model": "file-model", "timeout": 20}))
    provider = create_provider(config, environ={"OPENAI_API_KEY": "env-key", "LLM_MODEL": "env-model", "LLM_TIMEOUT": "12"},
                               model="explicit-model")
    assert provider.model == "explicit-model" and provider.timeout == 12
    assert provider._api_key == "env-key"
    assert create_provider(environ={"LLM_CONFIG_FILE": str(config)}).model == "file-model"


def test_missing_and_invalid_configuration(tmp_path):
    with pytest.raises(MissingLLMConfiguration):
        create_provider(environ={})
    with pytest.raises(LLMConfigurationError):
        create_provider(environ={"LLM_TIMEOUT": "broken"})
    with pytest.raises(LLMConfigurationError):
        create_provider(environ={"OPENAI_API_KEY": "key", "LLM_REQUIRE_API_KEY": "maybe"})
    config = tmp_path / "bad.json"
    for raw in ('{"unknown": "unit-secret"}', '[]', '{"api_key":'):
        config.write_text(raw)
        with pytest.raises(LLMConfigurationError) as caught:
            create_provider(config, environ={})
        assert "unit-secret" not in str(caught.value)


@pytest.mark.parametrize("agent_class,artifact,payload", [
    (ArchitectAgent, ARCHITECTURE, {"requirements": "cache"}),
    (CoderAgent, CODE, {"architecture": ARCHITECTURE}),
    (ReviewerAgent, {"review_status": "APPROVED"}, {"architecture": ARCHITECTURE, "code": CODE}),
])
def test_injected_provider_takes_priority_and_validates_artifacts(agent_class, artifact, payload):
    class MustNotRun:
        async def generate(self, request):
            pytest.fail("legacy generator must not run when provider is supplied")
    provider = provider_with(lambda request: httpx.Response(200, json=completion(json.dumps(artifact))))
    agent = agent_class(MustNotRun(), provider=provider)
    result = asyncio.run(agent.run(AgentInput(task_id="one", payload=payload)))
    assert result.status.value == "COMPLETED"
    provider.transport = httpx.MockTransport(lambda request: httpx.Response(200, json=completion('{}')))
    assert asyncio.run(agent.run(AgentInput(task_id="two", payload=payload))).status.value == "FAILED"


def test_provider_failure_is_failed_agent_not_fake_success():
    agent = ArchitectAgent(provider=provider_with(lambda request: httpx.Response(401, text="unit-secret")))
    result = asyncio.run(agent.run(AgentInput(task_id="one", payload={"requirements": "cache"})))
    assert result.status.value == "FAILED" and "LLMAuthenticationError" in result.error
    assert "unit-secret" not in result.error
    assert asyncio.run(ArchitectAgent().run(AgentInput(task_id="two", payload={"requirements": "cache"}))).status.value == "BLOCKED"


def test_mock_network_provider_runs_full_team_and_records_usage(tmp_path):
    artifacts = iter([ARCHITECTURE, CODE, {"review_status": "APPROVED"}])
    provider = provider_with(lambda request: httpx.Response(200, json=completion(json.dumps(next(artifacts)))))
    adapter = LLMArtifactGenerator(provider)
    result = asyncio.run(SoftwareTeamWorkflow(adapter, FakeExecutor(), tmp_path).run("cache"))
    assert result["status"] == "PASS" and result["execution_mode"] == "fake"
    assert [call["role"] for call in adapter.calls] == ["ARCHITECT", "CODER", "REVIEWER"]
    assert sum(call["token_usage"]["total_tokens"] for call in adapter.calls) == 54


def test_abstract_provider_and_content_only_adapter():
    with pytest.raises(TypeError):
        LLMProvider()
    class ContentProvider(LLMProvider):
        async def generate(self, **kwargs):
            return LLMResponse(content='{"ok": true}')
    adapter = LLMArtifactGenerator(ContentProvider())
    request = GenerationRequest(role=ARCHITECT, system_prompt="JSON", input_data={}, output_schema={})
    assert asyncio.run(adapter.generate(request)) == {"ok": True}


def test_live_demo_skips_without_credentials_and_strict_mode_fails(tmp_path, monkeypatch):
    from examples import live_llm_team_demo as demo
    monkeypatch.setattr(demo, "ROOT", tmp_path)
    monkeypatch.setattr(demo, "create_provider", lambda *args: create_provider(environ={}))
    assert demo.main([]) == 0
    assert demo.main(["--mode", "live"]) == 1
    summaries = [json.loads(path.read_text()) for path in tmp_path.glob("sandbox-validation/*/summary.json")]
    assert {summary["status"] for summary in summaries} == {"SKIPPED", "FAILED"}
    assert all("llm_calls" not in summary for summary in summaries)


def test_original_demo_forwards_llm_backend(monkeypatch):
    from examples import software_team_demo, live_llm_team_demo
    calls = []
    monkeypatch.setattr(live_llm_team_demo, "main", lambda argv: calls.append(argv) or 0)
    assert software_team_demo.main(["--backend=llm", "--mode", "live"]) == 0
    assert calls == [["--mode", "live"]]
