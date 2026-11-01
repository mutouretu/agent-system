"""Bridge model-neutral responses to the existing ArtifactGenerator protocol."""

import time

from .base import LLMProvider, LLMResponse, LLMResponseError
from .openai_provider import parse_json_object


class LLMArtifactGenerator:
    def __init__(self, provider: LLMProvider):
        self.provider = provider
        self.calls = []

    async def generate(self, request):
        record = {"role": request.role.value, "status": "FAILED"}
        self.calls.append(record)
        started = time.monotonic()
        try:
            response = await self.provider.generate(system_prompt=request.system_prompt,
                input_data=request.input_data, output_schema=request.output_schema)
            if not isinstance(response, LLMResponse):
                raise LLMResponseError("provider must return LLMResponse")
            result = response.structured_data
            if result is None:
                result = parse_json_object(response.content)
            record.update(status="COMPLETED", token_usage=dict(response.token_usage))
            return result
        finally:
            # Never store HTTP headers, keys, raw errors, or model content in metrics.
            record["elapsed_seconds"] = time.monotonic() - started
