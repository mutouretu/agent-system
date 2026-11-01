import json

import pytest

from scripts.validate_cli_loop import (
    DISABLED_FEATURES, MAX_RETRIES, audit_events, build_prompt,
    child_environment, codex_argv, parse_response,
)


def test_prompt_contains_complete_streams_contract_and_current_code():
    feedback = {"stdout": "assertion failure\nfull traceback\n", "stderr": "diagnostic stderr\n"}
    prompt = build_prompt("contract", "def candidate(): pass", feedback, "abc", 1)
    data = json.loads(prompt.split("\n\n", 1)[1])
    assert data["sandbox_feedback"] == feedback
    assert data["contract"] == "contract"
    assert data["current_candidate"] == "def candidate(): pass"
    assert data["feedback_sha256"] == "abc"
    assert data["max_retries"] == MAX_RETRIES == 3


def test_command_disables_execution_and_uses_existing_subscription(tmp_path):
    command = codex_argv("/trusted/codex", tmp_path / "schema", tmp_path / "response", tmp_path)
    assert command[:2] == ["/trusted/codex", "exec"]
    assert command[-1] == "-"
    assert command[command.index("--sandbox") + 1] == "read-only"
    assert 'forced_login_method="chatgpt"' in command
    assert "--ignore-user-config" in command
    assert "--ephemeral" in command
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    disabled = {command[i + 1] for i, value in enumerate(command[:-1]) if value == "--disable"}
    assert set(DISABLED_FEATURES) == disabled
    assert {"shell_tool", "code_mode_host", "multi_agent", "plugins", "hooks"} <= disabled


def test_api_keys_are_not_forwarded(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-only-placeholder")
    monkeypatch.setenv("CODEX_API_KEY", "test-only-placeholder")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://invalid.example")
    monkeypatch.setenv("CODEX_HOME", "/unchanged/auth-provider-home")
    environment = child_environment()
    assert "OPENAI_API_KEY" not in environment
    assert "CODEX_API_KEY" not in environment
    assert "OPENAI_BASE_URL" not in environment
    assert environment["CODEX_HOME"] == "/unchanged/auth-provider-home"


def response_file(tmp_path, **changes):
    value = {"candidate_source": "def candidate(): return 1", "reason": "fix failed assertion", "feedback_sha256": "expected"}
    value.update(changes)
    path = tmp_path / "response.json"
    path.write_text(json.dumps(value))
    return path


def test_parsed_candidate_remains_data(tmp_path):
    # Parsing must not execute the returned Python string.
    source = "raise RuntimeError('must never execute on the controller')"
    parsed = parse_response(response_file(tmp_path, candidate_source=source), "expected")
    assert parsed["candidate_source"] == source


@pytest.mark.parametrize("changes", [
    {"feedback_sha256": "stale"},
    {"candidate_source": ""},
    {"candidate_source": "bad\0source"},
    {"reason": None},
    {"extra": "unexpected field"},
])
def test_invalid_responses_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        parse_response(response_file(tmp_path, **changes), "expected")


def test_audit_accepts_completed_text_only_turn(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('\n'.join(json.dumps(event) for event in [
        {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}},
        {"type": "turn.completed", "usage": {"input_tokens": 100, "output_tokens": 20}},
    ]))
    assert audit_events(path)["tool_calls"] == 0


@pytest.mark.parametrize("item_type", ["command_execution", "mcp_tool_call", "file_change", "unknown_tool"])
def test_audit_rejects_tool_activity(tmp_path, item_type):
    path = tmp_path / "events.jsonl"
    path.write_text('\n'.join(json.dumps(event) for event in [
        {"type": "item.completed", "item": {"type": item_type}},
        {"type": "turn.completed", "usage": {}},
    ]))
    with pytest.raises(RuntimeError, match="unexpected CLI tool activity"):
        audit_events(path)


def test_incomplete_cli_turn_rejected(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"type":"thread.started"}\n')
    with pytest.raises(RuntimeError, match="no completed turn"):
        audit_events(path)


def test_recovered_transport_errors_are_not_terminal_failures(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('\n'.join(json.dumps(event) for event in [
        {"type": "error", "message": "Reconnecting..."},
        {"type": "item.completed", "item": {"type": "error", "message": "Falling back to HTTPS"}},
        {"type": "item.completed", "item": {"type": "agent_message", "text": "{}"}},
        {"type": "turn.completed", "usage": {"output_tokens": 20}},
    ]))
    result = audit_events(path)
    assert result["tool_calls"] == 0
    assert result["diagnostics"] == ["Reconnecting...", "Falling back to HTTPS"]


def test_terminal_failure_cannot_be_overridden_by_completion(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"type":"turn.failed"}\n{"type":"turn.completed","usage":{}}\n')
    with pytest.raises(RuntimeError, match="CLI turn failed"):
        audit_events(path)
