"""Four-role agent config contract. Stdlib only; no I/O, network, or model calls."""

from __future__ import annotations

import copy
import json
import re

ROLES = ("ARCHITECT", "CODER", "REVIEWER", "TESTER")
ROLE_SET = frozenset(ROLES)
BACKENDS = frozenset(("codex", "llm", "sandbox"))
SUPPORTED_TOOLS = frozenset(("read_file", "write_file", "apply_patch", "run_tests", "docker"))
SENSITIVE_FRAGMENTS = ("api_key", "token", "password", "secret")
_CREDENTIAL_RE = re.compile(r"^credential://[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_BASE_FIELDS = frozenset(("backend", "model", "responsibility", "tools", "credential_ref"))
_REQUIRED = frozenset(("backend", "responsibility"))
_LIST_REQUIRED = frozenset(("role", "backend", "responsibility"))
_LIST_FIELDS = _BASE_FIELDS | {"role"}
_MISSING = object()


def _fail(path: str, reason: str) -> None:
    raise ValueError(f"{path}: {reason}")


def _sensitive_map(node: dict, path: str) -> None:
    for key, value in node.items():
        if not isinstance(key, str):
            _fail(path, "keys must be strings")
        lowered = key.lower()
        for frag in SENSITIVE_FRAGMENTS:
            if frag in lowered:
                _fail(path, "sensitive key name is not allowed")
        _scan(value, f"{path}.{key}")


def _scan(node, path: str) -> None:
    if isinstance(node, dict):
        _sensitive_map(node, path)
    elif isinstance(node, (list, tuple)):
        for idx, value in enumerate(node):
            _scan(value, f"{path}[{idx}]")


def _check_keys(obj: dict, path: str, allowed: frozenset, required: frozenset) -> None:
    if not isinstance(obj, dict):
        _fail(path, "must be an object")
    _sensitive_map(obj, path)
    for key in obj:
        if key not in allowed:
            _fail(path, "unknown field")
    for key in sorted(required):
        if key not in obj:
            _fail(f"{path}.{key}", "missing required field")


def _clean_text(value, path: str, field: str) -> str:
    if not isinstance(value, str):
        _fail(path, f"{field} must be a string")
    cleaned = value.strip()
    if not cleaned:
        _fail(path, f"{field} must be non-empty")
    return cleaned


def _clean_model(record: dict, path: str, backend: str) -> str | None:
    model = record.get("model", _MISSING)
    if model is _MISSING:
        if backend == "sandbox":
            return None
        if backend == "codex":
            return "default"
        _fail(f"{path}.model", "model is required for this backend")
    if model is None:
        if backend == "sandbox":
            return None
        _fail(f"{path}.model", "model must be a string")
    if backend == "sandbox":
        _fail(f"{path}.model", "sandbox does not accept a model")
    return _clean_text(model, f"{path}.model", "model")


def _clean_tools(record: dict, path: str) -> list[str]:
    tools = record.get("tools", _MISSING)
    if tools is _MISSING:
        return []
    if not isinstance(tools, list):
        _fail(f"{path}.tools", "tools must be a list of strings")
    seen: set[str] = set()
    ordered: list[str] = []
    for idx, tool in enumerate(tools):
        tpath = f"{path}.tools[{idx}]"
        if not isinstance(tool, str):
            _fail(tpath, "tool name must be a string")
        if tool not in SUPPORTED_TOOLS:
            _fail(tpath, "unsupported tool name")
        if tool not in seen:
            seen.add(tool)
            ordered.append(tool)
    return ordered


def _clean_credential(record: dict, path: str) -> str | None:
    cred = record.get("credential_ref", _MISSING)
    if cred is _MISSING or cred is None:
        return None
    if not isinstance(cred, str) or not _CREDENTIAL_RE.match(cred):
        _fail(f"{path}.credential_ref", "must be null or a credential:// reference")
    return cred


def _normalize_record(record: dict, path: str) -> dict:
    _check_keys(record, path, _BASE_FIELDS, _REQUIRED)
    backend = record["backend"]
    if not isinstance(backend, str) or backend not in BACKENDS:
        _fail(f"{path}.backend", "unsupported backend")
    return {
        "backend": backend,
        "model": _clean_model(record, path, backend),
        "responsibility": _clean_text(
            record["responsibility"], f"{path}.responsibility", "responsibility"
        ),
        "tools": _clean_tools(record, path),
        "credential_ref": _clean_credential(record, path),
    }


def _collect_list(agents: list) -> dict:
    records: dict[str, tuple[dict, str]] = {}
    for idx, record in enumerate(agents):
        path = f"config.agents[{idx}]"
        _scan(record, path)
        _check_keys(record, path, _LIST_FIELDS, _LIST_REQUIRED)
        role = record["role"]
        if not isinstance(role, str):
            _fail(f"{path}.role", "role must be a string")
        if role not in ROLE_SET:
            _fail(f"{path}.role", "unknown role")
        if role in records:
            _fail(f"{path}.role", "duplicate role")
        records[role] = ({key: value for key, value in record.items() if key != "role"}, path)
    for role in ROLES:
        if role not in records:
            _fail("config.agents", f"missing role {role}")
    return {role: _normalize_record(*records[role]) for role in ROLES}


def _collect_mapping(agents: dict) -> dict:
    _scan(agents, "config.agents")
    for key in agents:
        if key not in ROLE_SET:
            _fail("config.agents", "unknown role")
    for role in ROLES:
        if role not in agents:
            _fail("config.agents", f"missing role {role}")
    return {role: _normalize_record(agents[role], f"config.agents.{role}") for role in ROLES}


def validate_agent_config(data) -> dict:
    """Validate and normalize a full four-role agent configuration."""
    try:
        json.dumps(data, allow_nan=False)
    except (TypeError, ValueError, OverflowError, RecursionError):
        _fail("config", "must be finite acyclic JSON data")
    _scan(data, "config")
    _check_keys(data, "config", frozenset(("agents",)), frozenset(("agents",)))
    agents = data["agents"]
    if isinstance(agents, list):
        normalized = _collect_list(agents)
    elif isinstance(agents, dict):
        normalized = _collect_mapping(agents)
    else:
        _fail("config.agents", "must be a list or object of role records")
    return {"agents": normalized}


def select_role_backend(config, role) -> dict:
    """Return backend, model, and responsibility for one validated role."""
    normalized = validate_agent_config(config)
    if not isinstance(role, str) or role not in ROLE_SET:
        _fail("role", "unknown role")
    record = normalized["agents"][role]
    return copy.deepcopy(
        {
            "backend": record["backend"],
            "model": record["model"],
            "responsibility": record["responsibility"],
        }
    )
