"""Trusted foreground worker. Only Codex transport and Docker SDK run on the host.

Claims via authenticated localhost HTTP; API never receives Docker or CLI credentials.
Ctrl-C stops claiming; an in-flight bounded command finishes before cleanup and exit.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import signal
import sys
import threading
import time
import uuid
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import docker
import httpx
from examples.software_team_demo import CodexArtifactGenerator
from rd_platform.llm import LLMArtifactGenerator, create_provider, LLMConfigurationError
from rd_platform.manager.software_team import SoftwareTeamWorkflow, save
from rd_platform.manager.role_router import RoleRouter
from rd_platform.agent_management import validate_agent_config
from rd_platform.domain.agent import AgentRole
from rd_platform.runtime.test_executor import DockerTestExecutor
from rd_platform.runtime.workspace_promoter import WorkspacePromoter
from rd_platform.agents.dev.schemas import CoderArtifacts
from rd_platform.agents.base import AgentInput, AgentStatus
from rd_platform.agents.dev.requirements import RequirementArchitectAgent
from rd_platform.agents.dev.interfaces import GenerationRequest

PYTHON_CONTRACT = """
Execution contract: Python 3.12, standard library only, no external services or network.
Put all business .py files under src/ and pytest .py files under tests/.
Tests import source modules directly (src is on the Python path). No conftest, plugins,
skipped tests, shell commands or dependency installation. Provide complete file contents.
"""

REPOSITORY_CONTRACT = """
Repository delivery contract: edit this rd_platform repository, not a standalone sample app.
Production files must be under src/rd_platform/ (.py, .js, .html, .css); add new tests
under tests/unit/ or tests/service/. Existing tests and the sandbox runtime are protected.
Return complete contents for each changed file. Preserve current API and UI behavior.
Prefer small new modules or pages over rewriting a large existing file.
Never overwrite an existing file when its supplied source is truncated or absent.
The candidate will run existing repository tests inside Docker before Git promotion.
"""


def repository_context(root, requirements, max_bytes=48000):
    """Read bounded, application-owned source context; never include config or credentials."""
    words = set(re.findall(r'[a-z][a-z_]{2,}', requirements.lower()))
    files = []
    for base in (root / 'src/rd_platform', root / 'tests/unit', root / 'tests/service'):
        for path in base.rglob('*'):
            if path.is_symlink() or not path.is_file() or path.suffix not in ('.py', '.js', '.html', '.css'):
                continue
            relative = path.relative_to(root).as_posix()
            files.append((relative, path))
    inventory = sorted(name for name, _ in files)
    ranked = []
    for name, path in files:
        if path.stat().st_size > 100000:
            continue
        body = path.read_text(encoding='utf-8')
        lower = body.lower()
        score = sum(10 if word in name.lower() else min(lower.count(word), 5) for word in words)
        if name.endswith(('service/api.py', 'service/static/index.html')):
            score += 15
        ranked.append((score, name, body))
    ui_task = any(term in requirements.lower() for term in (
        '页面', '侧边栏', '前端', '界面', 'dashboard', 'frontend', 'front-end', 'index.html'))
    priority = (
        'src/rd_platform/service/static/index.html',
        'src/rd_platform/service/api.py',
        'src/rd_platform/service/static/app.js',
        'src/rd_platform/service/static/app.css',
    ) if ui_task else ()
    priority_rank = {name: index for index, name in enumerate(priority)}
    ranked.sort(key=lambda item: (priority_rank.get(item[1], len(priority)), -item[0], item[1]))
    result = 'Repository file inventory:\n' + '\n'.join(inventory)[:8000] + '\nRelevant existing files:\n'
    for score, name, body in ranked:
        if (score <= 0 and name not in priority_rank) or len(result) >= max_bytes:
            break
        limit = 24000 if name in priority_rank and name != 'src/rd_platform/service/static/app.css' else 4000 if name in priority_rank else 16000
        excerpt = body[:max(0, min(len(body), limit, max_bytes - len(result) - len(name) - 70))]
        if not excerpt:
            break
        suffix = '\n[TRUNCATED: reference only; do not overwrite this file]' if len(excerpt) < len(body) else ''
        result += f'\n--- {name} ---\n{excerpt}{suffix}\n'
    return result[:max_bytes]


class WorkerConnectionError(RuntimeError):
    pass


class Worker:
    def __init__(self, api_url, token_file, *, once=False):
        url = urlsplit(api_url)
        if url.scheme != "http" or url.hostname not in ("127.0.0.1", "localhost") or url.username or url.query or url.fragment:
            raise ValueError("worker API must be a localhost HTTP URL")
        self.id = uuid.uuid4().hex
        self.directory = ROOT / "sandbox-validation" / f"workbench-worker-{self.id}"
        self.directory.mkdir(parents=True)
        config = self.directory / "docker-config"
        config.mkdir()
        os.environ["DOCKER_CONFIG"] = str(config)
        token_path = Path(token_file)
        if token_path.is_symlink():
            raise ValueError("worker token must be a regular application-owned file")
        token = token_path.read_text().strip()
        self.client = httpx.Client(base_url=api_url.rstrip("/"), timeout=15, trust_env=False,
                                   headers={"X-Worker-Token": token}, follow_redirects=False)
        self.stop = threading.Event()
        self.lease_lost = threading.Event()
        self.once = once
        self.backends = []

    def post(self, path, **data):
        for attempt in range(3):
            try:
                response = self.client.post("/internal/worker" + path, json={"worker_id": self.id, **data})
                if response.status_code < 500:
                    break
            except httpx.HTTPError:
                pass
            if attempt == 2:
                raise WorkerConnectionError("local API unavailable") from None
            time.sleep(1)
        if response.status_code != 200:
            raise RuntimeError(f"worker request rejected: {response.status_code}")
        return response.json()

    def preflight(self):
        client = docker.from_env(timeout=10)
        try:
            client.ping()
            client.images.get("python:3.12-slim")
        finally:
            client.close()
        if not list((ROOT / "sandbox-validation/wheels").glob("pytest-8.3.5-*.whl")):
            raise RuntimeError("offline pytest wheels are missing; follow README local validation setup")
        try:
            CodexArtifactGenerator(self.directory / "preflight-cli")
            self.backends.append("codex")
        except Exception as exc:
            print(f"Codex unavailable ({type(exc).__name__}); inspect preflight evidence", flush=True)
        try:
            create_provider()
            self.backends.append("llm")
        except LLMConfigurationError:
            pass
        if not self.backends:
            raise RuntimeError("No backend configured: sign into Codex with ChatGPT or configure LLM API credentials")
        available = set(self.backends)
        if {"codex", "llm"} <= available:
            self.backends.append("mixed")
        if available & {"codex", "llm"}:
            self.backends.append("configured")
        self.backends += ['prd_' + backend for backend in ("codex", "llm") if backend in available]
        self.post("/heartbeat", backends=self.backends)

    def heartbeat_loop(self):
        last_ok = time.monotonic()
        while not self.stop.wait(8):
            try:
                self.post("/heartbeat", backends=self.backends)
                last_ok = time.monotonic()
            except Exception:
                if time.monotonic() - last_ok > 40:
                    self.lease_lost.set()
                    print("API heartbeat unavailable; stopping after current bounded operation", flush=True)

    def run_job(self, job):
        if job['template'] == 'prd':
            return self.run_requirement(job)
        if job['template'] == 'chat':
            return self.run_chat(job)
        directory = self.directory / job["id"]
        directory.mkdir()
        summary = {"status": "FAILED"}
        executor = None
        try:
            requirements = job["requirements"]
            repository_mode = job.get('summary', {}).get('workspace_mode') == 'repository'
            promoter = WorkspacePromoter(ROOT) if repository_mode else None
            base_head = promoter.snapshot() if promoter is not None else None
            acceptance = None
            if job["template"] == "lru":
                fixtures = ROOT / "tests/fixtures/software_team"
                requirements = (fixtures / "requirements.txt").read_text()
                acceptance = {"tests/test_acceptance.py": (fixtures / "acceptance.py").read_text()}
            elif repository_mode:
                context_budget = max(0, min(48000, 64000 - len(requirements) - len(REPOSITORY_CONTRACT)))
                requirements += '\n' + REPOSITORY_CONTRACT + '\n' + repository_context(ROOT, requirements, context_budget)
            else:
                requirements += "\n" + PYTHON_CONTRACT
            if job["backend"] == "configured":
                snapshot = job.get("summary", {}).get("agent_config")
                if not isinstance(snapshot, dict):
                    raise ValueError("configured run has no Agent configuration snapshot")
                agents = validate_agent_config({"agents": snapshot["agents"]})["agents"]
                if agents["TESTER"]["backend"] != "sandbox":
                    raise ValueError("TESTER must use Docker sandbox")
                selected = {}
                backends = {}
                for role in (AgentRole.ARCHITECT, AgentRole.CODER, AgentRole.REVIEWER):
                    spec = agents[role.value]
                    backend, model = spec["backend"], spec["model"]
                    if backend == "codex":
                        selected[role] = CodexArtifactGenerator(directory / f"cli-{role.value.lower()}",
                            model=None if model == "default" else model)
                    elif backend == "llm":
                        selected[role] = LLMArtifactGenerator(create_provider(model=model))
                    else:
                        raise ValueError(f"{role.value} requires a model backend")
                    backends[role] = backend
                generator = RoleRouter(selected[AgentRole.ARCHITECT], selected[AgentRole.CODER],
                                       selected[AgentRole.REVIEWER], backends=backends,
                                       responsibilities={role: agents[role.value]["responsibility"] for role in backends})
            elif job["backend"] == "mixed":
                generator = RoleRouter(CodexArtifactGenerator(directory / "cli"),
                                       LLMArtifactGenerator(create_provider()))
            elif job["backend"] == "codex":
                generator = CodexArtifactGenerator(directory / "cli")
            else:
                generator = LLMArtifactGenerator(create_provider())
            executor = DockerTestExecutor(directory / "sandbox", ROOT / "sandbox-validation/wheels", acceptance_tests=acceptance,
                image='rd-platform-service:0.3.0-dev' if repository_mode else 'python:3.12-slim',
                timeout=120 if repository_mode else 30, repository_root=ROOT if repository_mode else None)

            async def event(kind, task):
                if self.stop.is_set() or self.lease_lost.is_set():
                    raise RuntimeError("worker stopping; workflow interrupted at role boundary")
                self.post(f"/runs/{job['id']}/events", generation=job["generation"], kind=kind, task=task)

            workflow = SoftwareTeamWorkflow(generator, executor, directory / "workflow", max_retries=job["max_retries"],
                                            on_event=event)
            seed = job.get('summary', {}).get('resume_seed') if job['template'] == 'resume' else None
            if job['template'] == 'resume' and not isinstance(seed, dict):
                raise ValueError('resume run has no checkpoint artifacts')
            summary = asyncio.run(workflow.run(requirements, resume_seed=seed,
                                               operator_guidance=job.get('summary', {}).get('operator_guidance')))
            if repository_mode and summary.get('status') == 'PASS':
                try:
                    candidate = CoderArtifacts.model_validate(summary['final_artifacts'])
                    tested_digest = executor.runs[-1]['receipt']['code_digest']
                    summary['delivery'] = promoter.promote(job['id'], candidate, base_head=base_head,
                        tested_digest=tested_digest, test_summary=summary['final_test_summary'],
                        execution_mode=summary['execution_mode'])
                    if summary['delivery']['status'] != 'COMMITTED':
                        summary.update(status='FAILED', delivery_status='COMMITTED_DIRTY',
                                       error='Git commit succeeded but the workspace changed concurrently')
                except Exception as exc:
                    summary.update(status='FAILED', delivery_status='BLOCKED',
                                   error=f'{type(exc).__name__}: repository promotion failed; inspect evidence')
            summary["calls"] = [{key: value for key, value in call.items() if key in ("role", "elapsed_seconds", "token_usage", "status")}
                                for call in generator.calls]
            summary["acceptance"] = "repository_regression" if repository_mode else "fixed_lru" if acceptance else "generated_tests_only"
        except Exception as exc:
            summary = {"status": "FAILED", "error": f"{type(exc).__name__}: worker operation failed; inspect local evidence"}
        finally:
            summary["evidence_dir"] = str(directory)
            if executor is not None:
                jobs = [item for run in executor.runs for item in run["jobs"]]
                summary["all_containers_removed"] = all(item["container_removed"] for item in jobs) if jobs else None
            save(directory / "summary.json", summary)
        self.post(f"/runs/{job['id']}/finish", generation=job["generation"], summary=summary)

    def run_chat(self, job):
        """Answer discussion or enqueue an explicit checkpoint continuation."""
        directory = self.directory / job['id']
        directory.mkdir(exist_ok=True)
        action = job.get('summary', {}).get('action')
        if isinstance(action, dict) and action.get('type') in ('resume', 'report_existing'):
            try:
                if action['type'] == 'report_existing':
                    existing_id = action['run_id']
                    test = action.get('final_test_summary') or {}
                    counts = test.get('counts') or {}
                    if action['status'] == 'COMPLETED':
                        outcome = f'Tester 已完成：{counts.get("passed", 0)}/{test.get("total", 0)} 项测试通过。'
                        if action.get('acceptance') == 'generated_tests_only':
                            outcome += '这些是生成代码附带的测试，尚不代表产物已部署到项目。'
                    else:
                        outcome = f'恢复运行仍处于 {action["status"]} 状态，无需重复启动。'
                    summary = {'status': 'CHAT_READY', 'resume_run_id': existing_id,
                               'answer': f'{outcome} 请进入运行 {existing_id[:8]} 查看最新证据。'}
                else:
                    parent_id = action['parent_run_id']
                    response = self.client.post(f'/v1/runs/{parent_id}/resume',
                        json={'task_id': action.get('task_id')})
                    if response.status_code != 202:
                        detail = response.json().get('detail', '恢复请求未被接受')
                        raise RuntimeError(str(detail))
                    resumed = response.json()
                    task_id = resumed['summary']['resume_from_task']
                    summary = {'status': 'CHAT_READY', 'resume_run_id': resumed['id'],
                               'answer': f'已从 {task_id} 节点创建恢复运行 {resumed["id"][:8]}。之前完成的产物会复用，后续阶段由研发团队继续执行。'}
            except Exception as exc:
                summary = {'status': 'FAILED', 'error': f'{type(exc).__name__}: 恢复运行失败，请在运行详情中手动选择节点重试'}
            summary['evidence_dir'] = str(directory)
            save(directory / 'summary.json', summary)
            self.post(f"/runs/{job['id']}/finish", generation=job['generation'], summary=summary)
            return
        try:
            backend = job['backend']
            model = None
            if backend == 'configured':
                snapshot = job.get('summary', {}).get('agent_config')
                if not snapshot:
                    raise ValueError('chat is missing its project Agent configuration')
                spec = validate_agent_config({'agents': snapshot['agents']})['agents']['ARCHITECT']
                backend, model = spec['backend'], spec['model']
            elif backend == 'mixed':
                backend = 'codex'
            if backend == 'codex':
                generator = CodexArtifactGenerator(directory / 'cli', model=None if model in (None, 'default') else model)
            elif backend == 'llm':
                generator = LLMArtifactGenerator(create_provider(**({'model': model} if model else {})))
            else:
                raise ValueError('chat requires a model backend')
            prompt = GenerationRequest(role=AgentRole.ARCHITECT,
                system_prompt=('You are the Team Lead answering a human question about ONE EXISTING software R&D run. '
                    'The input contains the original request, role outcomes, exact failure evidence, related resumed runs newest first, and prior discussion. '
                    'Use the newest related run as the current execution state. If its Tester passed, state the measured result and do not claim a Tester failure. '
                    'Distinguish generated-test success from independent acceptance and from deployment of artifacts. '
                    'Answer the question directly in Chinese. Treat a question about a failure as discussion, never as a new feature request. '
                    'Do not claim you ran tests or changed code. Do not reveal credentials. '
                    'Return only JSON with an answer string of at most 8000 characters.'),
                input_data={'run_context': job['summary'].get('chat_context', ''), 'question': job['requirements']},
                output_schema={'type': 'object', 'properties': {'answer': {'type': 'string'}},
                               'required': ['answer'], 'additionalProperties': False})
            result = asyncio.run(generator.generate(prompt))
            answer = result.get('answer') if isinstance(result, dict) else None
            if not isinstance(answer, str) or not 1 <= len(answer.strip()) <= 8000:
                raise ValueError('model did not return a bounded answer')
            summary = {'status': 'CHAT_READY', 'answer': answer.strip()}
        except Exception as exc:
            summary = {'status': 'FAILED', 'error': f'{type(exc).__name__}: Team Lead 回复失败，请检查 Worker 证据'}
        summary['evidence_dir'] = str(directory)
        save(directory / 'summary.json', summary)
        self.post(f"/runs/{job['id']}/finish", generation=job['generation'], summary=summary)

    def run_requirement(self, job):
        directory = self.directory / job['id']
        directory.mkdir(exist_ok=True)
        try:
            generator = (CodexArtifactGenerator(directory / 'cli') if job['backend'] == 'prd_codex'
                         else LLMArtifactGenerator(create_provider()))
            agent = RequirementArchitectAgent(generator)
            result = asyncio.run(agent.run(AgentInput(task_id=job['id'], payload=json.loads(job['requirements']))))
            if result.status != AgentStatus.COMPLETED:
                raise RuntimeError('requirement generation failed')
            summary = {'status': 'PRD_READY', 'prd': result.artifacts, 'evidence_dir': str(directory)}
        except Exception as exc:
            summary = {'status': 'FAILED', 'error': f'{type(exc).__name__}: Architect 解析失败，请检查 Worker 证据', 'evidence_dir': str(directory)}
        save(directory / 'summary.json', summary)
        self.post(f"/runs/{job['id']}/finish", generation=job['generation'], summary=summary)

    def run(self):
        try:
            self.preflight()
            thread = threading.Thread(target=self.heartbeat_loop, name="worker-heartbeat", daemon=True)
            thread.start()
            print(f"Worker ready: {self.id}; backends={','.join(self.backends)}", flush=True)
            try:
                while not self.stop.is_set() and not self.lease_lost.is_set():
                    try:
                        job = self.post("/claim")["run"]
                    except WorkerConnectionError:
                        self.stop.wait(2)
                        continue
                    if job is not None:
                        print(f"Claimed {job['id']}", flush=True)
                        self.run_job(job)
                        if self.once:
                            break
                    else:
                        self.stop.wait(2)
            finally:
                self.stop.set()
                thread.join(timeout=20)
                try:
                    self.post("/heartbeat", backends=[])
                except Exception:
                    pass
        finally:
            self.client.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:" + os.environ.get("API_PORT", "8000"))
    parser.add_argument("--token-file", default=str(ROOT / "data/.workbench-worker-token"))
    parser.add_argument("--once", action="store_true", help="process one queued run, then exit")
    args = parser.parse_args()
    worker = Worker(args.api_url, args.token_file, once=args.once)
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: worker.stop.set())
    worker.run()


if __name__ == "__main__":
    main()
