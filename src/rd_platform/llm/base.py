"""Model-neutral generation contracts. Providers return data, never execute tools."""

from abc import ABC, abstractmethod
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class LLMError(RuntimeError):
    """Safe-to-display provider error; excludes credentials and response bodies."""


class LLMConfigurationError(LLMError):
    pass


class MissingLLMConfiguration(LLMConfigurationError):
    pass


class LLMAuthenticationError(LLMError):
    pass


class LLMRateLimitError(LLMError):
    pass


class LLMTimeoutError(LLMError):
    pass


class LLMResponseError(LLMError):
    pass


class LLMResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    content: str
    structured_data: dict[str, Any] | None = None
    # Missing provider usage remains {}, rather than invented zero usage.
    token_usage: dict[str, Annotated[StrictInt, Field(ge=0)]] = Field(default_factory=dict)


class LLMProvider(ABC):
    @abstractmethod
    async def generate(self, *, system_prompt: str, input_data: dict[str, Any],
                       output_schema: dict[str, Any]) -> LLMResponse:
        """Return one JSON artifact. No command execution or automatic tool calls."""
        raise NotImplementedError
