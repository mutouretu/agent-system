"""Trusted API controller; all generated code/tests run only inside Docker.

PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python examples/live_llm_team_demo.py
Missing credentials => SKIPPED (auto); --mode live makes this a configuration failure.
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
sys.path.insert(0, str(ROOT / "src"))

from rd_platform.llm import create_provider, LLMArtifactGenerator, LLMConfigurationError, MissingLLMConfiguration
from rd_platform.manager.software_team import SoftwareTeamWorkflow, save
from rd_platform.runtime.test_executor import DockerTestExecutor


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("auto", "live"), default="auto")
    parser.add_argument("--config", help="explicit JSON config; env overrides file values")
    parser.add_argument("--max-retries", type=int, choices=range(4), default=3)
    args = parser.parse_args(argv)
    directory = ROOT / "sandbox-validation" / f"live-llm-team-{uuid.uuid4().hex}"
    directory.mkdir(parents=True)
    summary = {"status": "FAILED", "generator": "openai-compatible-api", "evidence_dir": str(directory)}
    started = time.monotonic()
    generator = executor = None
    try:
        try:
            provider = create_provider(args.config)
        except MissingLLMConfiguration:
            if args.mode == "live":
                raise
            summary.update(status="SKIPPED", reason="LLM API credentials are not configured; no model call or Docker test was run")
            return 0
        config = directory / "docker-config"
        config.mkdir()
        os.environ["DOCKER_CONFIG"] = str(config)
        requirements = (ROOT / "tests/fixtures/software_team/requirements.txt").read_text()
        acceptance = (ROOT / "tests/fixtures/software_team/acceptance.py").read_text()
        summary["acceptance_sha256"] = hashlib.sha256(acceptance.encode()).hexdigest()
        save(directory / "contract.json", {"requirements": requirements, "acceptance_sha256": summary["acceptance_sha256"]})
        generator = LLMArtifactGenerator(provider)
        executor = DockerTestExecutor(directory / "sandbox", ROOT / "sandbox-validation/wheels",
                                      acceptance_tests={"tests/test_acceptance.py": acceptance})
        workflow = SoftwareTeamWorkflow(generator, executor, directory / "workflow", max_retries=args.max_retries,
                                        task_timeout=max(180, provider.timeout + 30))
        result = asyncio.run(workflow.run(requirements))
        summary.update(result)
        summary["evidence_dir"] = str(directory)
        return 0 if result["status"] == "PASS" else 1
    except LLMConfigurationError as exc:
        summary["error"] = str(exc)
        return 1
    except Exception as exc:
        # Do not dump arbitrary exception bodies: they may contain request secrets.
        summary["error"] = f"{type(exc).__name__}: live demo failed; inspect workflow and sandbox evidence"
        return 1
    finally:
        summary["elapsed_seconds"] = time.monotonic() - started
        if generator is not None:
            summary["llm_calls"] = generator.calls
            summary["llm_total_seconds"] = sum(call["elapsed_seconds"] for call in generator.calls)
            summary["token_usage"] = {key: sum(call.get("token_usage", {}).get(key, 0) for call in generator.calls)
                                      for key in {key for call in generator.calls for key in call.get("token_usage", {})}}
        if executor is not None:
            jobs = [job for run in executor.runs for job in run["jobs"]]
            summary["all_containers_removed"] = all(job["container_removed"] for job in jobs) if jobs else None
        save(directory / "summary.json", summary)
        print(json.dumps({key: value for key, value in summary.items() if key != "final_artifacts"},
                         ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
