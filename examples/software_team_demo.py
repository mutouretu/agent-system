"""Trusted local controller: real subscription CLI generation and Docker-only tests.

PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python examples/software_team_demo.py
Requires docker==7.1.0 and pydantic==2.14.2 in the control virtualenv.
"""

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from scripts.validate_cli_loop import audit_events, codex_argv, invoke, read_limited, save, select_cli
from rd_platform.manager.software_team import SoftwareTeamWorkflow
from rd_platform.runtime.test_executor import DockerTestExecutor

TRANSPORT_SCHEMA = {"type": "object", "properties": {"artifact_json": {"type": "string"}},
                    "required": ["artifact_json"], "additionalProperties": False}


def decode_artifact(raw):
    outer = json.loads(raw)
    if not isinstance(outer, dict) or set(outer) != {"artifact_json"} or not isinstance(outer["artifact_json"], str):
        raise ValueError("CLI must return exactly one artifact_json string")
    artifact = json.loads(outer["artifact_json"])
    if not isinstance(artifact, dict):
        raise ValueError("artifact_json must encode an object")
    return artifact


class CodexArtifactGenerator:
    """Uses the existing audited CLI transport; never reads authentication files."""
    def __init__(self, directory, *, executable=None, model=None, timeout=180):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True)
        self.workspace = self.directory / "empty-workspace"
        self.workspace.mkdir()
        self.executable = select_cli(self.directory, executable)
        self.model, self.timeout, self.calls = model, timeout, []
        login = invoke([self.executable, "login", "status"], self.directory, "login", timeout=20)
        text = read_limited(self.directory / "login.stdout") + read_limited(self.directory / "login.stderr")
        if login["exit_code"] or "Logged in using ChatGPT" not in text:
            raise RuntimeError("the selected Codex CLI must have an active ChatGPT subscription login")
        self.schema = self.directory / "transport-schema.json"
        save(self.schema, TRANSPORT_SCHEMA)

    async def generate(self, request):
        number = len(self.calls) + 1
        directory = self.directory / f"{number:02d}-{request.role.value.lower()}"
        directory.mkdir()
        prompt = (
            "You are one role in a software R&D team. Do not call any tools, read local files, run shell commands, "
            "execute code/tests, install packages, access services, or spawn agents. Use only supplied data. "
            "TRANSPORT: return {\"artifact_json\": \"JSON-encoded artifact object\"}. "
            "The role instructions and artifact_schema below apply to the INNER object encoded in artifact_json. "
            "Return complete file content where requested. Do not claim tests were executed.\n\n"
            + json.dumps({"role_instructions": request.system_prompt, "input_data": request.input_data,
                          "artifact_schema": request.output_schema}, ensure_ascii=False, indent=2)
        )
        (directory / "prompt.txt").write_text(prompt, encoding="utf-8")
        response = directory / "response.json"
        print(f"Codex generation: {request.role.value}, call {number}", flush=True)
        call = invoke(codex_argv(self.executable, self.schema, response, self.workspace, self.model), directory,
                      "codex", prompt=prompt, timeout=self.timeout, cwd=self.workspace)
        self.calls.append({"role": request.role.value, **call})
        if call["exit_code"] or call["timed_out"]:
            raise RuntimeError(f"Codex failed for {request.role.value}; inspect {directory}")
        call["audit"] = audit_events(directory / "codex.stdout")
        self.calls[-1]["audit"] = call["audit"]
        artifact = decode_artifact(read_limited(response, 2 * 1024 * 1024))
        save(directory / "artifact.json", artifact)
        return artifact


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex")
    parser.add_argument("--model")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--max-retries", type=int, choices=range(4), default=3)
    args = parser.parse_args()
    if not 1 <= args.timeout <= 600:
        parser.error("timeout must be in [1, 600]")
    directory = ROOT / "sandbox-validation" / f"software-team-{uuid.uuid4().hex}"
    directory.mkdir(parents=True)
    config = directory / "docker-config"
    config.mkdir()
    os.environ["DOCKER_CONFIG"] = str(config)
    fixtures = ROOT / "tests/fixtures/software_team"
    requirements = (fixtures / "requirements.txt").read_text()
    acceptance = (fixtures / "acceptance.py").read_text()
    save(directory / "contract.json", {"requirements": requirements, "acceptance_sha256": hashlib.sha256(acceptance.encode()).hexdigest()})
    summary = {"status": "FAILED", "evidence_dir": str(directory), "generator": "codex-subscription-cli"}
    generator = executor = None
    started = time.monotonic()
    print(f"Evidence directory: {directory}", flush=True)
    try:
        generator = CodexArtifactGenerator(directory / "cli", executable=args.codex, model=args.model, timeout=args.timeout)
        executor = DockerTestExecutor(directory / "sandbox", ROOT / "sandbox-validation/wheels",
                                      acceptance_tests={"tests/test_acceptance.py": acceptance})
        workflow = SoftwareTeamWorkflow(generator, executor, directory / "workflow", max_retries=args.max_retries,
                                        task_timeout=max(180, args.timeout + 30))
        result = asyncio.run(workflow.run(requirements))
        summary.update(result)
        summary["evidence_dir"] = str(directory)
        summary["acceptance_sha256"] = hashlib.sha256(acceptance.encode()).hexdigest()
        return 0 if result["status"] == "PASS" else 1
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        print(summary["error"], file=sys.stderr, flush=True)
        return 1
    finally:
        summary["elapsed_seconds"] = time.monotonic() - started
        if generator is not None:
            summary["cli_calls"] = generator.calls
            summary["cli_total_seconds"] = sum(call["elapsed_seconds"] for call in generator.calls)
        if executor is not None:
            summary["all_containers_removed"] = bool(executor.runs) and all(
                job["container_removed"] for run in executor.runs for job in run["jobs"])
        save(directory / "summary.json", summary)
        print(json.dumps({key: value for key, value in summary.items() if key not in ("cli_calls", "final_artifacts")},
                         indent=2, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
