"""Explicit overrides > environment > JSON config > defaults. No dotenv/auth reads."""

import json
import os
from pathlib import Path

from .base import LLMConfigurationError
from .openai_provider import OpenAIProvider

ENV_FIELDS = {
    "OPENAI_API_KEY": "api_key", "LLM_BASE_URL": "base_url", "LLM_MODEL": "model",
    "LLM_TIMEOUT": "timeout", "LLM_MAX_TOKENS": "max_tokens",
    "LLM_RESPONSE_FORMAT": "response_format", "LLM_REQUIRE_API_KEY": "require_api_key",
    "LLM_ENABLE_THINKING": "enable_thinking",
    "LLM_STREAM": "stream",
}


def create_provider(config_file=None, *, environ=None, **overrides):
    env = os.environ if environ is None else environ
    path = config_file if config_file is not None else env.get("LLM_CONFIG_FILE")
    config = {}
    if path:
        try:
            # Config is data only; never imports Python or evaluates expressions.
            with Path(path).open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError()
            config = json.loads(raw)
            if not isinstance(config, dict):
                raise ValueError()
        except (OSError, ValueError, TypeError):
            raise LLMConfigurationError("LLM config must be a readable JSON object of at most 64 KiB") from None
    if set(config) - (set(ENV_FIELDS.values()) | {"provider"}):
        raise LLMConfigurationError("unknown LLM config fields")
    for name, field in ENV_FIELDS.items():
        if name in env:
            config[field] = env[name]
    config.update(overrides)
    if config.pop("provider", "openai") != "openai":
        raise LLMConfigurationError("unsupported provider; use openai for OpenAI-compatible APIs")
    try:
        if isinstance(config.get("timeout"), str):
            config["timeout"] = float(config["timeout"])
        if isinstance(config.get("max_tokens"), str):
            config["max_tokens"] = int(config["max_tokens"])
        if isinstance(config.get("require_api_key"), str):
            value = config["require_api_key"].lower()
            if value not in ("true", "false"):
                raise ValueError()
            config["require_api_key"] = value == "true"
        if isinstance(config.get("enable_thinking"), str):
            value = config["enable_thinking"].lower()
            if value not in ("true", "false"):
                raise ValueError()
            config["enable_thinking"] = value == "true"
        if isinstance(config.get("stream"), str):
            value = config["stream"].lower()
            if value not in ("true", "false"):
                raise ValueError()
            config["stream"] = value == "true"
    except ValueError:
        raise LLMConfigurationError("invalid LLM_TIMEOUT, LLM_MAX_TOKENS, LLM_REQUIRE_API_KEY, LLM_ENABLE_THINKING or LLM_STREAM") from None
    if set(config) - (set(ENV_FIELDS.values()) | {"transport"}):
        raise LLMConfigurationError("unknown provider overrides")
    return OpenAIProvider(**config)
