"""Trusted Docker controller for probe stress, paired benchmarking and pytest.

PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python scripts/validate_probe.py
Host code only stages inputs, manages Docker and receives structured artifacts.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]


def save(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    run_dir = ROOT / "sandbox-validation" / f"probe-{uuid.uuid4().hex}"
    workspace, inputs, output, config = (run_dir / name for name in ("workspace", "input", "output", "docker-config"))
    for directory in (workspace, inputs, output, config):
        directory.mkdir(parents=True)
    output.chmod(0o777)
    os.environ["DOCKER_CONFIG"] = str(config)
    sys.path.insert(0, str(ROOT / "src"))
    import docker
    from docker.errors import NotFound
    from rd_platform.runtime.sandbox import DockerSandbox

    shutil.copytree(ROOT / "src", workspace / "src", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(ROOT / "sandbox-validation/wheels", inputs / "wheels")
    for name in ("workload.py", "benchmark.py"):
        shutil.copy2(ROOT / "tests/fixtures/probing" / name, workspace / name)
    shutil.copy2(ROOT / "tests/unit/test_probe.py", workspace / "test_probe.py")
    shutil.copy2(ROOT / "tests/integration/test_probe_evidence.py", workspace / "test_probe_evidence.py")
    (workspace / "pytest.ini").write_text("[pytest]\nmarkers =\n    integration: real sandbox probe evidence\n")

    client = docker.from_env(timeout=10)
    box = DockerSandbox(
        os.environ.get("SANDBOX_IMAGE", "python:3.12-slim"), workspace, inputs, output,
        client=client, timeout_seconds=120, max_output_bytes=64 * 1024,
    )
    record = {"container_name": box.name, "container_removed": False}
    print(f"Evidence directory: {run_dir}", flush=True)
    try:
        with box:
            container = client.containers.get(box.container_id)
            record["container_id"] = box.container_id
            record["image_id"] = container.attrs["Image"]
            record["limits"] = {key: container.attrs["HostConfig"][key] for key in (
                "NetworkMode", "ReadonlyRootfs", "NanoCpus", "Memory", "PidsLimit",
            )}
            print("Running 10,000-row fault probe and seven alternating benchmark pairs inside Docker", flush=True)
            code, stdout, stderr = box.exec_run([
                "/usr/bin/env", "PYTHONPATH=/workspace/src:/workspace",
                "python", "/workspace/benchmark.py",
            ])
            save(run_dir / "benchmark-execution.json", {"exit_code": code, "stdout": stdout, "stderr": stderr})
            if code:
                print(stderr, file=sys.stderr, flush=True)
                return code
            record["wire_bytes"] = len(stdout.encode("utf-8")) + len(stderr.encode("utf-8"))
            record["context_sha256"] = hashlib.sha256(stdout.encode("utf-8")).hexdigest()
            context = json.loads(stdout)
            benchmark = json.loads((output / "benchmark.json").read_text())
            code, stdout, stderr = box.exec_run([
                "python", "-m", "pip", "install", "--no-index", "--no-compile",
                "--disable-pip-version-check", "--find-links=/input/wheels",
                "--target=/tmp/deps", "pytest==8.3.5",
            ])
            save(run_dir / "dependency-install.json", {"exit_code": code, "stdout": stdout, "stderr": stderr})
            if code:
                print(stderr, file=sys.stderr, flush=True)
                return code
            print("Running probe unit and evidence assertions in the isolated container", flush=True)
            code, stdout, stderr = box.exec_run([
                "/usr/bin/env", "PYTHONPATH=/tmp/deps:/workspace/src:/workspace",
                "PROBE_EVIDENCE_DIR=/output", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "--junitxml=/output/pytest.xml", "/workspace/test_probe.py", "/workspace/test_probe_evidence.py",
            ])
            save(run_dir / "pytest-execution.json", {"exit_code": code, "stdout": stdout, "stderr": stderr})
            record["pytest_exit_code"] = code
            print(stdout, end="", flush=True)
            print(stderr, end="", file=sys.stderr, flush=True)
            raw_bytes = sum(log["bytes_seen"] for log in context["logs"].values())
            summary = {
                "passed": code == 0,
                "raw_log_bytes": raw_bytes,
                "context_bytes": record["wire_bytes"],
                "context_token_upper_bound": context["budget"]["token_upper_bound"],
                "token_count_method": context["budget"]["token_count_method"],
                "context_vs_raw_log_reduction": 1 - record["wire_bytes"] / raw_bytes,
                "function_sampling": context["sampling"],
                "retained_call_samples": len(context["samples"]),
                "log_samples": len(context["logs"]["stdout"]["samples"]),
                "exception_type": context["exception"]["type"],
                "exception_frames": [frame["function"] for frame in context["exception"]["frames"]],
                "exception_locals_trimmed": context["budget"]["exception_locals_trimmed"],
                "baseline_median_seconds": benchmark["baseline_median_seconds"],
                "probed_median_seconds": benchmark["probed_median_seconds"],
                "median_overhead_ratio": benchmark["median_overhead_ratio"],
                "median_added_seconds": benchmark["median_added_seconds"],
                "baseline_rows_per_second": benchmark["baseline_rows_per_second"],
                "probed_rows_per_second": benchmark["probed_rows_per_second"],
            }
            save(run_dir / "summary.json", summary)
            print(json.dumps(summary, indent=2, ensure_ascii=False), flush=True)
        return code
    finally:
        try:
            box.cleanup()
            try:
                client.containers.get(box.name)
            except NotFound:
                record["container_removed"] = True
            save(run_dir / "lifecycle.json", record)
            if not record["container_removed"]:
                raise RuntimeError("sandbox was not removed")
        finally:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
