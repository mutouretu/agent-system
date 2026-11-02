"""Trusted Docker lifecycle controller; NEVER executes tests/code on the host.

Stages files, sends commands to Docker, collects structured evidence, and returns
the exit status produced by pytest INSIDE the final isolated container. No Docker
socket, host environment or credentials are passed into any execution container.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "sandbox-validation"
# Do not read ~/.docker/config.json or invoke credential helpers.
CONFIG = STATE / "docker-config"
CONFIG.mkdir(parents=True, exist_ok=True)
os.environ["DOCKER_CONFIG"] = str(CONFIG)
sys.path.insert(0, str(ROOT / "src"))

import docker
from docker.errors import ImageNotFound, NotFound

from rd_platform.runtime.sandbox import DockerSandbox, SandboxError


def is_removed(client, container_id):
    try:
        client.containers.get(container_id)
        return False
    except NotFound:
        return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pytest-path", default="tests", help="path under tests/ to run inside Docker")
    args = parser.parse_args(argv)
    selected = (ROOT / args.pytest_path).resolve()
    if selected != ROOT / "tests" and not selected.is_relative_to(ROOT / "tests"):
        parser.error("--pytest-path must be under tests/")
    if not selected.exists():
        parser.error("--pytest-path does not exist")
    image = os.environ.get("SANDBOX_IMAGE", "python:3.12-slim")
    run_dir = STATE / f"run-{uuid.uuid4().hex}"
    workspace, inputs, output = (run_dir / name for name in ("workspace", "input", "output"))
    for path in (workspace, inputs, output):
        path.mkdir(parents=True)
    # Only this newly created, empty artifact directory is made writable by the
    # fixed container UID. No system directories or existing permissions change.
    output.chmod(0o777)
    for name in ("src", "tests", "scripts", "examples"):
        shutil.copytree(ROOT / name, workspace / name, ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copy2(ROOT / "pyproject.toml", workspace / "pyproject.toml")
    shutil.copytree(STATE / "wheels", inputs / "wheels")
    (inputs / "fixture.txt").write_text("sandbox input\n")
    evidence = {}
    boxes = []
    probe_ids = []
    client = docker.from_env(timeout=10)
    try:
        client.ping()
        try:
            base_image = client.images.get(image)
        except ImageNotFound:
            print(f"Provisioning public Linux image via Docker SDK: {image}", flush=True)
            base_image = client.images.pull(image)

        # /tmp is intentionally noexec. Native framework dependencies must live
        # in an immutable image layer, not the writable test dependency target.
        build_dir = run_dir / "image"
        build_dir.mkdir()
        shutil.copytree(inputs / "wheels", build_dir / "wheels")
        (build_dir / "Dockerfile").write_text(
            f"FROM {base_image.id}\n"
            "USER 0:0\n"
            "COPY wheels/ /opt/validation-wheels/\n"
            "RUN python -m pip install --no-index --no-cache-dir --no-compile "
            "--find-links=/opt/validation-wheels pydantic==2.14.2 httpx==0.28.1 "
            "&& rm -rf /opt/validation-wheels\n"
        )
        print("Preparing native framework dependencies in a network-disabled Docker image", flush=True)
        prepared, _ = client.images.build(path=str(build_dir), network_mode="none", rm=True, forcerm=True,
                                          tag="rd-platform-validation:0.3.0")
        image = prepared.id
        (run_dir / "image.json").write_text(json.dumps({"base_image_id": base_image.id, "image_id": image,
                                                       "build_network": "none"}, indent=2))

        def create_box(**options):
            box = DockerSandbox(image, workspace, inputs, output, client=client, **options)
            boxes.append(box)
            return box

        print("Collecting real sandbox evidence via Docker SDK", flush=True)
        box = create_box()
        with box:
            probe_ids.append(box.container_id)
            state = client.containers.get(box.container_id).attrs
            evidence["image_id"] = state["Image"]
            evidence["host_config"] = state["HostConfig"]
            evidence["probe"] = box.exec_run(["python", "/workspace/tests/probes/runtime_probe.py"])
            evidence["artifact"] = box.exec_run(["cat", "/output/result.txt"])
        evidence["normal_removed"] = is_removed(client, probe_ids[-1])

        for key, options, command in (
            ("timeout", {"timeout_seconds": 1}, ["/bin/sh", "-c", "sleep 300 & wait"]),
            ("flood", {"max_output_bytes": 1024}, ["python", "-c", "import os; os.write(1, b'x' * 65536)"]),
        ):
            box = create_box(**options)
            with box:
                probe_ids.append(box.container_id)
                started = time.monotonic()
                record = {"error": None}
                try:
                    record["result"] = box.exec_run(command)
                except SandboxError as error:
                    record["error"] = type(error).__name__
                record["elapsed"] = time.monotonic() - started
            record["removed"] = is_removed(client, probe_ids[-1])
            evidence[key] = record

        evidence["remaining_probe_containers"] = [item for item in probe_ids if not is_removed(client, item)]
        (inputs / "evidence.json").write_text(json.dumps(evidence, indent=2))
        print("Running unit and integration assertions inside network-disabled Docker sandbox", flush=True)
        with create_box() as box:
            code, stdout, stderr = box.exec_run([
                "python", "-m", "pip", "install", "--no-index", "--no-compile", "--disable-pip-version-check",
                "--find-links=/input/wheels", "--target=/tmp/deps", "docker==7.1.0", "pytest==8.3.5",
            ])
            if code:
                print(stdout + stderr, flush=True)
                return code
            code, stdout, stderr = box.exec_run([
                "/usr/bin/env", "PYTHONPATH=/tmp/deps:/workspace/src:/workspace",
                "SANDBOX_EVIDENCE=/input/evidence.json", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "--junitxml=/output/pytest.xml", "/workspace/" + str(selected.relative_to(ROOT)),
            ])
            print(stdout, end="", flush=True)
            print(stderr, end="", file=sys.stderr, flush=True)
        print(f"Evidence and reports: {run_dir}", flush=True)
        return code
    finally:
        # Attempt every cleanup even if one container cannot be removed. Never
        # hide failures or leave later containers unattempted.
        failures = []
        for box in reversed(boxes):
            try:
                box.cleanup()
            except Exception as error:
                failures.append(f"{box.name}: {error}")
        client.close()
        if failures:
            raise RuntimeError("cleanup failed: " + "; ".join(failures))


if __name__ == "__main__":
    raise SystemExit(main())
