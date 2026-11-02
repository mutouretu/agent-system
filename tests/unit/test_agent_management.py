import copy
import json

import pytest

from rd_platform.agent_management import select_role_backend, validate_agent_config


def sample_list():
    return {"agents": [
        {"role": "ARCHITECT", "backend": "codex", "responsibility": " 需求拆解与架构设计 "},
        {"role": "CODER", "backend": "llm", "model": "deepseek-v4.1-flash", "responsibility": "实现代码与单测"},
        {"role": "REVIEWER", "backend": "llm", "model": "deepseek-v4.1-flash", "responsibility": "静态审查与自纠建议"},
        {"role": "TESTER", "backend": "sandbox", "responsibility": "执行 Docker 测试并采集证据"},
    ]}


def sample_mapping():
    return {"agents": {
        "ARCHITECT": {"backend": "codex", "responsibility": "需求拆解与架构设计"},
        "CODER": {"backend": "llm", "model": "deepseek-v4.1-flash", "responsibility": "实现代码与单测"},
        "REVIEWER": {"backend": "llm", "model": "deepseek-v4.1-flash", "responsibility": "静态审查与自纠建议"},
        "TESTER": {"backend": "sandbox", "responsibility": "执行 Docker 测试并采集证据"},
    }}


def test_valid_list_and_mapping_equal():
    a = validate_agent_config(sample_list())
    b = validate_agent_config(sample_mapping())
    assert a == b
    assert list(a["agents"]) == ["ARCHITECT", "CODER", "REVIEWER", "TESTER"]
    assert a["agents"]["ARCHITECT"]["model"] == "default"
    assert a["agents"]["TESTER"]["model"] is None
    assert a["agents"]["ARCHITECT"]["responsibility"] == "需求拆解与架构设计"
    json.dumps(a)


def test_input_not_modified_and_no_alias():
    data = sample_list()
    original = copy.deepcopy(data)
    out = validate_agent_config(data)
    assert data == original
    out["agents"]["CODER"]["backend"] = "sandbox"
    out["agents"]["ARCHITECT"]["tools"].append("docker")
    assert sample_list() == original


def test_tools_no_alias_with_input_tools():
    data = sample_mapping()
    data["agents"]["CODER"]["tools"] = ["read_file", "write_file"]
    original_tools = list(data["agents"]["CODER"]["tools"])
    out = validate_agent_config(data)
    assert out["agents"]["CODER"]["tools"] == original_tools
    assert out["agents"]["CODER"]["tools"] is not data["agents"]["CODER"]["tools"]
    out["agents"]["CODER"]["tools"].append("docker")
    assert data["agents"]["CODER"]["tools"] == original_tools


def test_idempotent_revalidation():
    once = validate_agent_config(sample_list())
    assert once == validate_agent_config(once)
    json.dumps(once)


@pytest.mark.parametrize("role", ["ARCHITECT", "CODER", "REVIEWER", "TESTER"])
def test_missing_role(role):
    data = sample_list()
    data["agents"] = [r for r in data["agents"] if r["role"] != role]
    with pytest.raises(ValueError):
        validate_agent_config(data)
    mapping = sample_mapping()
    del mapping["agents"][role]
    with pytest.raises(ValueError):
        validate_agent_config(mapping)


def test_extra_and_duplicate_role():
    extra = sample_list()
    extra["agents"].append({"role": "HACKER", "backend": "codex", "responsibility": "x"})
    with pytest.raises(ValueError):
        validate_agent_config(extra)
    dup = sample_list()
    dup["agents"].append({"role": "ARCHITECT", "backend": "codex", "responsibility": "x"})
    with pytest.raises(ValueError, match="duplicate"):
        validate_agent_config(dup)


@pytest.mark.parametrize("mutate", [
    lambda d: d.update({"extra": 1}),
    lambda d: d["agents"][0].update({"unknown": 1}),
    lambda d: d["agents"][0].update({"backend": "gpt"}),
    lambda d: d["agents"][0].update({"responsibility": "   "}),
    lambda d: d["agents"][1].update({"model": 5}),
    lambda d: d["agents"][1].update({"model": None}),
    lambda d: d["agents"][3].update({"model": "x"}),
    lambda d: d["agents"][1].update({"tools": ["browse"]}),
    lambda d: d["agents"][1].update({"tools": "read_file"}),
    lambda d: d["agents"][1].update({"tools": None}),
    lambda d: d["agents"][1].update({"tools": [1]}),
    lambda d: d["agents"][1].update({"tools": [{"x": 1}]}),
    lambda d: d["agents"][1].update({"credential_ref": "credential://a b"}),
    lambda d: d["agents"][1].update({"credential_ref": "plain-secret"}),
    lambda d: d.update({1: "x"}),
    lambda d: d["agents"][1].update({2: "y"}),
])
def test_invalid_inputs(mutate):
    data = sample_list()
    mutate(data)
    with pytest.raises(ValueError):
        validate_agent_config(data)


def test_credential_null_accepted():
    data = sample_mapping()
    data["agents"]["CODER"]["credential_ref"] = None
    out = validate_agent_config(data)
    assert out["agents"]["CODER"]["credential_ref"] is None


def test_non_dict_record_rejected():
    with pytest.raises(ValueError):
        validate_agent_config({"agents": ["ARCHITECT", "CODER", "REVIEWER", "TESTER"]})
    with pytest.raises(ValueError):
        validate_agent_config({"agents": {r: "x" for r in ("ARCHITECT", "CODER", "REVIEWER", "TESTER")}})


def test_cyclic_input_and_nonfinite_numbers_are_rejected_cleanly():
    data = sample_mapping()
    data["agents"]["ARCHITECT"]["tools"] = []
    data["agents"]["ARCHITECT"]["tools"].append(data)
    with pytest.raises(ValueError, match="finite acyclic JSON"):
        validate_agent_config(data)
    bad = sample_mapping()
    bad["agents"]["CODER"]["model"] = float("nan")
    with pytest.raises(ValueError, match="finite acyclic JSON"):
        validate_agent_config(bad)


@pytest.mark.parametrize("key", ["api_key", "TOKEN", "Password", "my_SECRET"])
def test_sensitive_keys(key):
    data = sample_mapping()
    data["agents"]["ARCHITECT"][key] = "redacted"
    with pytest.raises(ValueError, match="sensitive"):
        validate_agent_config(data)


def test_nested_sensitive_key_rejected():
    data = sample_mapping()
    data["agents"]["ARCHITECT"]["tools"] = [{"api_key": "redacted"}]
    with pytest.raises(ValueError, match="sensitive"):
        validate_agent_config(data)


def test_omitted_sandbox_model_and_tools_defaults():
    out = validate_agent_config(sample_mapping())
    assert out["agents"]["TESTER"]["model"] is None
    assert out["agents"]["TESTER"]["tools"] == []
    assert out["agents"]["CODER"]["tools"] == []


def test_explicit_sandbox_null_model_matches_architecture_contract():
    data = sample_mapping()
    data["agents"]["TESTER"]["model"] = None
    assert validate_agent_config(data)["agents"]["TESTER"]["model"] is None
    for role in ("ARCHITECT", "CODER"):
        bad = sample_mapping()
        bad["agents"][role]["model"] = None
        with pytest.raises(ValueError):
            validate_agent_config(bad)


def test_valid_credential_reference_and_tool_dedup():
    data = sample_mapping()
    data["agents"]["CODER"]["credential_ref"] = "credential://deepseek.prod-1"
    data["agents"]["CODER"]["tools"] = ["read_file", "read_file", "write_file"]
    out = validate_agent_config(data)
    assert out["agents"]["CODER"]["credential_ref"] == "credential://deepseek.prod-1"
    assert out["agents"]["CODER"]["tools"] == ["read_file", "write_file"]


def test_select_role_backend():
    data = sample_list()
    original = copy.deepcopy(data)
    config = validate_agent_config(data)
    config["agents"]["CODER"]["credential_ref"] = "credential://x"
    result = select_role_backend(config, "CODER")
    assert set(result) == {"backend", "model", "responsibility"}
    assert result["backend"] == "llm"
    assert result["responsibility"] == "实现代码与单测"
    assert "credential://x" not in json.dumps(result)
    assert data == original
    result["backend"] = "codex"
    assert select_role_backend(config, "CODER")["backend"] == "llm"


def test_select_rejects_bad_role_and_config():
    config = validate_agent_config(sample_list())
    for role in ("coder", "ADMIN", None, 1):
        with pytest.raises(ValueError):
            select_role_backend(config, role)
    bad = sample_mapping()
    del bad["agents"]["TESTER"]
    with pytest.raises(ValueError):
        select_role_backend(bad, "CODER")
