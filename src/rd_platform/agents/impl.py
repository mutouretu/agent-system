"""Small deterministic examples: no model calls, shell, eval or dynamic imports."""

import math

from rd_platform.domain import AgentMetadata, Task
from .base import BaseAgent


def _values(task: Task, context: dict) -> list[int | float]:
    data = task.input.data
    if "values" not in data:
        dependencies = context.get("dependencies", {})
        source = data.get("source_task")
        if source is None and len(dependencies) == 1:
            source = next(iter(dependencies))
        if not isinstance(source, str) or source not in dependencies:
            raise ValueError("provide values or select a direct dependency with source_task")
        data = dependencies[source]
    values = data.get("values")
    if not isinstance(values, list) or len(values) > 10000:
        raise ValueError("values must be a list containing at most 10000 numbers")
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("values must contain finite numbers, excluding booleans")
    return list(values)


class DataPipelineAgent(BaseAgent):
    def __init__(self):
        super().__init__(AgentMetadata("data-pipeline", "Data Pipeline", frozenset({"data_processing"})))

    async def run(self, task: Task, context: dict) -> dict:
        values = sorted(set(_values(task, context)))
        return {"values": values, "count": len(values)}


class AlgorithmRunnerAgent(BaseAgent):
    def __init__(self):
        super().__init__(AgentMetadata("algorithm-runner", "Algorithm Runner", frozenset({"algorithm_execution"})))

    async def run(self, task: Task, context: dict) -> dict:
        values = _values(task, context)
        total = sum(values)
        return {"count": len(values), "sum": total, "min": min(values) if values else None,
                "max": max(values) if values else None, "mean": total / len(values) if values else None}
