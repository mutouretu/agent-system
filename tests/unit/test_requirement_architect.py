import asyncio
from rd_platform.agents.base import AgentInput
from rd_platform.agents.dev.requirements import RequirementArchitectAgent


class Generator:
    def __init__(self, result): self.result = result
    async def generate(self, request):
        assert request.role.value == 'ARCHITECT'
        assert 'document' in request.output_schema['properties']
        return self.result


def test_architect_rejects_prose_and_unknown_priority():
    agent = RequirementArchitectAgent(Generator('not JSON'))
    assert asyncio.run(agent.run(AgentInput(task_id='r', payload={}))).status == 'FAILED'


def test_architect_generates_validated_prd():
    doc = {'title': '需求', 'business_context': '业务目标', 'feature_packages': [{'id': 'P1', 'name': '功能', 'description': '说明', 'priority': 'P0', 'group': '核心'}], 'acceptance_criteria': ['结果可验收']}
    result = {'document': doc, 'reply': '请确认边界', 'risks': ['缺少超时上限'], 'completeness_score': 80}
    agent = RequirementArchitectAgent(Generator(result))
    output = asyncio.run(agent.run(AgentInput(task_id='r', payload={'prompt': '需求'})))
    assert output.status == 'COMPLETED' and output.artifacts['document'] == doc
    doc['feature_packages'][0]['priority'] = 'P9'
    assert asyncio.run(agent.run(AgentInput(task_id='r', payload={}))).status == 'FAILED'


def test_requirement_worker_only_generates_structured_data(tmp_path, monkeypatch):
    import json
    from scripts import workbench_worker as module
    doc = {'title': '需求', 'business_context': '目标', 'feature_packages': [{'id': 'P1', 'name': '功能', 'description': '说明', 'priority': 'P0', 'group': '核心'}], 'acceptance_criteria': ['结果可验收']}
    generator = Generator({'document': doc, 'reply': '已拆解', 'risks': [], 'completeness_score': 100})
    monkeypatch.setattr(module, 'CodexArtifactGenerator', lambda path: generator)
    def forbidden(*args, **kwargs): raise AssertionError('PRD must not execute code')
    monkeypatch.setattr(module, 'DockerTestExecutor', forbidden)
    worker = object.__new__(module.Worker)
    worker.directory = tmp_path
    posted = []
    worker.post = lambda path, **body: posted.append((path, body))
    worker.run_job({'id': 'r', 'template': 'prd', 'backend': 'prd_codex', 'generation': 1, 'requirements': '{}'})
    assert posted[0][1]['summary']['status'] == 'PRD_READY'
    assert json.loads((tmp_path/'r/summary.json').read_text())['prd']['document'] == doc
