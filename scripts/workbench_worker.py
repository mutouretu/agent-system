"""Trusted foreground worker. Only Codex transport and Docker SDK run on the host.

Claims via authenticated localhost HTTP; API never receives Docker or CLI credentials.
Ctrl-C stops claiming; an in-flight bounded command finishes before cleanup and exit.
"""

import argparse
import asyncio
import json
import os
from pathlib import Path
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
from rd_platform.runtime.test_executor import DockerTestExecutor
from rd_platform.agents.base import AgentInput, AgentStatus
from rd_platform.agents.dev.requirements import RequirementArchitectAgent

PYTHON_CONTRACT = """
Execution contract: Python 3.12, standard library only, no external services or network.
Put all business .py files under src/ and pytest .py files under tests/.
Tests import source modules directly (src is on the Python path). No conftest, plugins,
skipped tests, shell commands or dependency installation. Provide complete file contents.
"""


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
        directory = self.directory / job["id"]
        directory.mkdir()
        summary = {"status": "FAILED"}
        executor = None
        try:
            requirements = job["requirements"]
            acceptance = None
            if job["template"] == "lru":
                fixtures = ROOT / "tests/fixtures/software_team"
                requirements = (fixtures / "requirements.txt").read_text()
                acceptance = {"tests/test_acceptance.py": (fixtures / "acceptance.py").read_text()}
            else:
                requirements += "\n" + PYTHON_CONTRACT
            if job["backend"] == "mixed":
                generator = RoleRouter(CodexArtifactGenerator(directory / "cli"),
                                       LLMArtifactGenerator(create_provider()))
            elif job["backend"] == "codex":
                generator = CodexArtifactGenerator(directory / "cli")
            else:
                generator = LLMArtifactGenerator(create_provider())
            executor = DockerTestExecutor(directory / "sandbox", ROOT / "sandbox-validation/wheels", acceptance_tests=acceptance)

            async def event(kind, task):
                if self.stop.is_set() or self.lease_lost.is_set():
                    raise RuntimeError("worker stopping; workflow interrupted at role boundary")
                self.post(f"/runs/{job['id']}/events", generation=job["generation"], kind=kind, task=task)

            workflow = SoftwareTeamWorkflow(generator, executor, directory / "workflow", max_retries=job["max_retries"],
                                            on_event=event)
            summary = asyncio.run(workflow.run(requirements))
            summary["calls"] = [{key: value for key, value in call.items() if key in ("role", "elapsed_seconds", "token_usage", "status")}
                                for call in generator.calls]
            summary["acceptance"] = "fixed_lru" if acceptance else "generated_tests_only"
        except Exception as exc:
            summary = {"status": "FAILED", "error": f"{type(exc).__name__}: worker operation failed; inspect local evidence"}
        finally:
            summary["evidence_dir"] = str(directory)
            if executor is not None:
                jobs = [item for run in executor.runs for item in run["jobs"]]
                summary["all_containers_removed"] = all(item["container_removed"] for item in jobs) if jobs else None
            save(directory / "summary.json", summary)
        self.post(f"/runs/{job['id']}/finish", generation=job["generation"], summary=summary)

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
