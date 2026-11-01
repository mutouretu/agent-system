"""Resumable single-agent feedback loop; task execution and pytest are Docker-only.

init: execute the intentionally flawed initial candidate.
retry RUN_DIR CANDIDATE --diagnosis FILE: accept one agent revision based on feedback.
status RUN_DIR: display recorded convergence, without executing anything.

There are no canned fixes or simulated model outputs. The current conversation
agent supplies revisions after receiving full feedback. No API agent is claimed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/self_correction"
MAX_RETRIES = 3
EXPECTED_TESTS = 24


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def summary(state):
    attempts = state["attempts"]
    rates = [attempt["metrics"]["pass_rate"] for attempt in attempts]
    final_pass = bool(attempts and attempts[-1]["passed"])
    monotonic = all(right >= left for left, right in zip(rates, rates[1:]))
    strict = len(rates) > 1 and all(right > left for left, right in zip(rates, rates[1:]))
    return {
        "agent_mode": state["agent_mode"],
        "max_retries": MAX_RETRIES,
        "execution_count": len(attempts),
        "iteration_count": max(0, len(attempts) - 1),
        "pass_rates": rates,
        "monotonic_nondecreasing": monotonic,
        "strictly_increasing": strict,
        "final_pass": final_pass,
        "self_correction_pass": bool(final_pass and len(attempts) > 1 and not attempts[0]["passed"] and monotonic),
        "all_containers_removed": all(attempt["container_removed"] for attempt in attempts),
    }


def parse_report(path):
    # Parse only structured evidence, never candidate code, on the control plane.
    if not path.is_file() or path.stat().st_size > 1024 * 1024:
        raise RuntimeError("missing or oversized JUnit report")
    cases = ET.parse(path).getroot().findall(".//testcase")
    metrics = {"total": len(cases), "passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    outcomes = []
    for case in cases:
        status = "passed"
        for tag, key in (("error", "errors"), ("failure", "failed"), ("skipped", "skipped")):
            if case.find(tag) is not None:
                status = key
                break
        metrics[status] += 1
        outcomes.append({"name": case.attrib["name"], "status": status})
    # Denominator stays fixed even on collection failures or missing tests.
    metrics["pass_rate"] = metrics["passed"] / EXPECTED_TESTS
    metrics["outcomes"] = outcomes
    return metrics


def execute(run_dir, state, candidate, diagnosis=None):
    index = len(state["attempts"])
    if index > MAX_RETRIES:
        raise RuntimeError("max_retries=3 exhausted; no further sandbox execution allowed")
    if index and state["attempts"][-1]["passed"]:
        raise RuntimeError("the loop has already converged")
    if digest(run_dir / "contract.py") != state["suite_sha256"]:
        raise RuntimeError("the frozen test suite was modified")
    if index:
        previous = run_dir / state["attempts"][-1]["feedback_file"]
        if not diagnosis or diagnosis.get("feedback_sha256") != digest(previous):
            raise RuntimeError("revision must reference the full immediately preceding feedback")
        if not diagnosis.get("reason"):
            raise RuntimeError("revision must explain the observed failure and intended fix")

    attempt_dir = run_dir / f"iteration-{index:02d}"
    attempt_dir.mkdir()  # Never overwrite an existing attempt after a crash.
    workspace, inputs, output = (attempt_dir / name for name in ("workspace", "input", "output"))
    for directory in (workspace, inputs, output):
        directory.mkdir()
    output.chmod(0o777)  # New task artifacts only; no host system permission changes.
    shutil.copy2(candidate, workspace / "candidate.py")
    shutil.copy2(run_dir / "contract.py", workspace / "test_contract.py")
    shutil.copytree(ROOT / "sandbox-validation/wheels", inputs / "wheels")
    if diagnosis:
        write_json(attempt_dir / "agent-diagnosis.json", diagnosis)

    config = run_dir / "docker-config"
    config.mkdir(exist_ok=True)
    os.environ["DOCKER_CONFIG"] = str(config)
    sys.path.insert(0, str(ROOT / "src"))
    import docker
    from docker.errors import NotFound
    from rd_platform.runtime.sandbox import DockerSandbox

    client = docker.from_env(timeout=10)
    box = DockerSandbox(state["image_id"], workspace, inputs, output, client=client, timeout_seconds=120)
    record = {
        "iteration": index,
        "candidate_sha256": digest(workspace / "candidate.py"),
        "suite_sha256": state["suite_sha256"],
        "container_name": box.name,
        "container_removed": False,
    }
    started = time.monotonic()
    try:
        with box:
            record["container_id"] = box.container_id
            config = client.containers.get(box.container_id).attrs["HostConfig"]
            record["isolation"] = {key: config[key] for key in (
                "NetworkMode", "ReadonlyRootfs", "NanoCpus", "Memory", "PidsLimit",
            )}
            code, stdout, stderr = box.exec_run([
                "python", "-m", "pip", "install", "--no-index", "--no-compile",
                "--disable-pip-version-check", "--find-links=/input/wheels",
                "--target=/tmp/deps", "pytest==8.3.5",
            ])
            write_json(attempt_dir / "dependency-install.json", {"exit_code": code, "stdout": stdout, "stderr": stderr})
            if code:
                raise RuntimeError("offline pytest provisioning failed; see dependency-install.json")
            code, stdout, stderr = box.exec_run([
                "/usr/bin/env", "PYTHONPATH=/tmp/deps:/workspace", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "python", "-m", "pytest", "-q", "--tb=long", "--showlocals", "--color=no",
                "-p", "no:cacheprovider", "--junitxml=/output/pytest.xml", "/workspace/test_contract.py",
            ])
            record.update({"exit_code": code, "stdout": stdout, "stderr": stderr})
            (attempt_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
            (attempt_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
    except BaseException as error:
        write_json(attempt_dir / "controller-error.json", {"type": type(error).__name__, "message": str(error)})
        raise
    finally:
        try:
            box.cleanup()
            try:
                client.containers.get(box.name)
            except NotFound:
                record["container_removed"] = True
            write_json(attempt_dir / "cleanup.json", {"name": box.name, "removed": record["container_removed"]})
            if not record["container_removed"]:
                raise RuntimeError("container still exists after cleanup")
        finally:
            client.close()

    record["elapsed_seconds"] = time.monotonic() - started
    record["metrics"] = parse_report(output / "pytest.xml")
    record["passed"] = code == 0 and record["metrics"]["total"] == EXPECTED_TESTS and record["metrics"]["passed"] == EXPECTED_TESTS
    record["feedback_file"] = str((attempt_dir / "feedback.json").relative_to(run_dir))
    write_json(attempt_dir / "feedback.json", record)
    state["attempts"].append(record)
    write_json(run_dir / "state.json", state)
    result = summary(state)
    write_json(run_dir / "summary.json", result)
    print(stdout, end="", flush=True)
    print(stderr, end="", file=sys.stderr, flush=True)
    print(json.dumps(result, indent=2), flush=True)
    print(f"RUN_DIR={run_dir}", flush=True)
    print(f"FEEDBACK_SHA256={digest(attempt_dir / 'feedback.json')}", flush=True)
    return 0 if record["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    init = subparsers.add_parser("init")
    init.add_argument("--image", default="python:3.12-slim")
    init.add_argument("--run-dir", type=Path)
    init.add_argument("--agent-mode", default="current-conversation-single-agent; no canned repairs or mock model")
    retry = subparsers.add_parser("retry")
    retry.add_argument("run_dir", type=Path)
    retry.add_argument("candidate", type=Path)
    retry.add_argument("--diagnosis", type=Path, required=True)
    status = subparsers.add_parser("status")
    status.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    if args.command == "init":
        run_dir = args.run_dir.resolve() if args.run_dir else ROOT / "sandbox-validation" / f"self-correction-{uuid.uuid4().hex}"
        run_dir.mkdir(parents=True)
        for name in ("contract.py", "task.md", "initial.py"):
            shutil.copy2(FIXTURES / name, run_dir / name)
        config = run_dir / "docker-config"
        config.mkdir()
        os.environ["DOCKER_CONFIG"] = str(config)
        import docker
        client = docker.from_env(timeout=10)
        try:
            image_id = client.images.get(args.image).id
        finally:
            client.close()
        state = {
            "agent_mode": args.agent_mode,
            "max_retries": MAX_RETRIES,
            "image_id": image_id,
            "suite_sha256": digest(run_dir / "contract.py"),
            "attempts": [],
        }
        write_json(run_dir / "state.json", state)
        return execute(run_dir, state, run_dir / "initial.py")
    run_dir = args.run_dir.resolve(strict=True)
    state = json.loads((run_dir / "state.json").read_text())
    if args.command == "status":
        print(json.dumps(summary(state), indent=2))
        return 0
    return execute(run_dir, state, args.candidate, json.loads(args.diagnosis.read_text()))


if __name__ == "__main__":
    raise SystemExit(main())
