"""Shared validation, dependency resolution and structured generation."""

import logging
import json

from pydantic import ValidationError
from rd_platform.agents.base import AgentInput, AgentOutput, AgentStatus, BaseAgent
from rd_platform.domain.task import json_object
from .interfaces import ArtifactGenerator, GenerationRequest
from .prompts import PROMPTS
from rd_platform.llm import LLMArtifactGenerator, LLMProvider, LLMResponseError

logger = logging.getLogger(__name__)


def input_payload(request: AgentInput, allowed: set[str]) -> dict:
    payload = json_object(request.payload)
    if set(payload) - (allowed | {"artifact_sources"}):
        raise ValueError("unsupported role input fields")
    sources = payload.get("artifact_sources", {})
    if not isinstance(sources, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in sources.items()):
        raise ValueError("artifact_sources must map artifact names to dependency task IDs")
    if set(sources) - allowed:
        raise ValueError("unknown artifact source name")
    return payload


def resolve(request, payload, key, schema):
    if key in payload:
        return schema.model_validate(payload[key])
    dependencies = request.context.get("dependencies", {})
    if not isinstance(dependencies, dict):
        raise ValueError("dependencies must be a mapping of upstream outputs")
    source = payload.get("artifact_sources", {}).get(key)
    if source is not None:
        if source not in dependencies:
            raise ValueError(f"artifact source {source} is not a direct dependency")
        return schema.model_validate(dependencies[source])
    matches = []
    for artifact in dependencies.values():
        try:
            matches.append(schema.model_validate(artifact))
        except ValidationError:
            continue
    if len(matches) != 1:
        raise ValueError(f"{key} requires exactly one valid upstream artifact or an explicit artifact_sources selection")
    return matches[0]


def failed(exc):
    return AgentOutput(status=AgentStatus.FAILED, error=f"{type(exc).__name__}: {exc}"[:4096])


class GeneratingAgent(BaseAgent):
    def __init__(self, metadata, generator: ArtifactGenerator | None, *, provider: LLMProvider | None = None):
        super().__init__(metadata)
        self.generator = LLMArtifactGenerator(provider) if provider is not None else generator

    def unavailable(self):
        return AgentOutput(status=AgentStatus.BLOCKED, error="artifact generator is not configured")

    async def generate(self, data, schema, *, repair_format=False):
        prompt = GenerationRequest(role=self.metadata.role, system_prompt=PROMPTS[self.metadata.role],
                                   input_data=json_object(data), output_schema=schema.model_json_schema())
        try:
            result = await self.generator.generate(prompt)
            if not isinstance(result, dict):
                raise ValueError("generator must return a JSON object, not prose or a code fence")
            try:
                return schema.model_validate(json_object(result))
            except ValidationError as error:
                if not repair_format:
                    raise
                # One bounded repair of the model's JSON shape. Semantic and
                # safety checks still run against the full strict contract.
                feedback = [{"path": ".".join(map(str, item["loc"])), "type": item["type"]}
                            for item in error.errors(include_input=False)[:20]]
                retry = prompt.model_copy(update={
                    "system_prompt": prompt.system_prompt + "\nYour prior JSON failed schema validation. Return a complete corrected object. Do not omit required fields or add unknown keys.",
                    "input_data": {**prompt.input_data, "format_feedback": feedback,
                                   "previous_json_excerpt": json.dumps(result, ensure_ascii=False)[:12000]},
                })
                repaired = await self.generator.generate(retry)
                if not isinstance(repaired, dict):
                    raise ValueError("repair must return a JSON object")
                return schema.model_validate(json_object(repaired))
        except (ValidationError, LLMResponseError, ValueError) as exc:
            # Record only the failure class. Model text may contain project data or secrets.
            logger.warning("agent_artifact_rejected role=%s schema=%s error_type=%s",
                           self.metadata.role.value, schema.__name__, type(exc).__name__)
            raise
