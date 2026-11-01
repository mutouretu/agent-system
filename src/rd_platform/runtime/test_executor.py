"""Trusted Docker lifecycle controller for Python artifacts; never imports them."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import time
import uuid
from xml.etree import ElementTree

import docker
from docker.errors import NotFound

from rd_platform.agents.dev.schemas import CoderArtifacts, SandboxTestResult, TestCounts, code_digest, safe_path
from .sandbox import DockerSandbox


RUNNER = '''import os, sys
os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
sys.path.insert(0, "/tmp/deps")
import pytest
# Candidate source cannot shadow pytest or the standard library during bootstrap.
sys.path.append("/workspace/src")
raise SystemExit(pytest.main([
    "-q", "-c", "/input/pytest.ini", "--noconftest", "-p", "no:cacheprovider",
    "--junitxml=/output/pytest.xml", "/workspace/tests",
]))
'''


def parse_junit(raw: bytes) -> TestCounts:
    if len(raw) > 1024 * 1024 or b"<!DOCTYPE" in raw.upper() or b"<!ENTITY" in raw.upper():
        raise ValueError("unsafe or oversized JUnit report")
    root = ElementTree.fromstring(raw)
    if root.tag not in ("testsuites", "testsuite"):
        raise ValueError("invalid JUnit report root")
    counts = {"passed": 0, "failed": 0, "errors": 0, "skipped": 0}
    for case in root.iter("testcase"):
        outcomes = [tag for tag in ("failure", "error", "skipped") if case.find(tag) is not None]
        if len(outcomes) > 1:
            raise ValueError("inconsistent JUnit testcase outcomes")
        key = {"failure": "failed", "error": "errors", "skipped": "skipped"}.get(outcomes[0], "passed") if outcomes else "passed"
        counts[key] += 1
    return TestCounts(**counts)


def read_report(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 1024 * 1024:
            raise ValueError("JUnit must be a bounded regular file")
        raw = stream.read(1024 * 1024 + 1)
    return raw


def save(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


class DockerTestExecutor:
    """Two independent containers: generated unit tests, then immutable acceptance.

    Only Python files under src/ and tests/ are accepted in this first executor.
    Lifecycle calls are synchronous inside run_tests; DockerSandbox enforces its
    own hard timeout/output cap, including when the asyncio loop is occupied.
    """

    def __init__(self, evidence_dir, wheels_dir, *, acceptance_tests=None, image="python:3.12-slim", timeout=30, client=None):
        self.evidence_dir = Path(evidence_dir)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        self.wheels_dir = Path(wheels_dir).resolve(strict=True)
        self.acceptance_tests = dict(acceptance_tests or {})
        for path, content in self.acceptance_tests.items():
            self._python_path(path, "tests/")
            if not isinstance(content, str):
                raise ValueError("acceptance test contents must be strings")
        self.image, self.timeout, self.client = image, timeout, client
        self.runs = []

    @staticmethod
    def _python_path(path, prefix):
        safe_path(path)
        if not path.startswith(prefix) or not path.endswith(".py"):
            raise ValueError(f"Python executor requires .py files under {prefix}")

    async def run_tests(self, code: CoderArtifacts) -> SandboxTestResult:
        code = CoderArtifacts.model_validate(code.model_dump())
        for path in code.files:
            self._python_path(path, "src/")
        for path in code.tests:
            self._python_path(path, "tests/")
        run_id = f"tests-{uuid.uuid4().hex}"
        run_dir = self.evidence_dir / run_id
        run_dir.mkdir()
        record = {"run_id": run_id, "code_digest": code_digest(code), "jobs": [], "status": "ERROR"}
        self.runs.append(record)
        save(run_dir / "candidate.json", code.model_dump())
        client = self.client or docker.from_env(timeout=10)
        started = time.monotonic()
        try:
            # Pin one image ID across both jobs and keep the image identity in evidence.
            image = client.images.get(self.image)
            record["image_id"] = image.id
            jobs = [("unit", code.tests)]
            if self.acceptance_tests:
                jobs.append(("acceptance", self.acceptance_tests))
            for name, tests in jobs:
                job_dir = run_dir / name
                workspace, inputs, output = (job_dir / label for label in ("workspace", "input", "output"))
                for path in (workspace, inputs, output):
                    path.mkdir(parents=True)
                output.chmod(0o777)
                for path, content in {**code.files, **tests}.items():
                    target = workspace / path
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with target.open("x", encoding="utf-8") as stream:
                        stream.write(content)
                manifest = {path: hashlib.sha256(content.encode()).hexdigest() for path, content in {**code.files, **tests}.items()}
                save(job_dir / "manifest.json", manifest)
                shutil.copytree(self.wheels_dir, inputs / "wheels")
                (inputs / "run_tests.py").write_text(RUNNER)
                (inputs / "pytest.ini").write_text("[pytest]\naddopts =\n")
                box = DockerSandbox(image.id, workspace, inputs, output, client=client,
                                    timeout_seconds=self.timeout, max_output_bytes=24 * 1024)
                job = {"name": name, "container_removed": False, "manifest": manifest}
                record["jobs"].append(job)
                print(f"Docker tests: {name} ({run_id})", flush=True)
                try:
                    with box:
                        job["container_id"] = box.container_id
                        state = client.containers.get(box.container_id).attrs
                        job["limits"] = {key: state["HostConfig"][key] for key in (
                            "NetworkMode", "ReadonlyRootfs", "NanoCpus", "Memory", "PidsLimit",
                        )}
                        setup = box.exec_run(["python", "-I", "-m", "pip", "install", "--no-index", "--no-compile",
                                              "--disable-pip-version-check", "--find-links=/input/wheels",
                                              "--target=/tmp/deps", "pytest==8.3.5"])
                        save(job_dir / "setup.json", dict(zip(("exit_code", "stdout", "stderr"), setup)))
                        if setup[0]:
                            raise RuntimeError("offline pytest installation failed; inspect setup.json")
                        execution = box.exec_run(["python", "-I", "/input/run_tests.py"])
                        save(job_dir / "execution.json", dict(zip(("exit_code", "stdout", "stderr"), execution)))
                        job["exit_code"], job["stdout"], job["stderr"] = execution
                        counts = parse_junit(read_report(output / "pytest.xml"))
                        job["counts"] = counts.model_dump()
                        if execution[0] == 0 and (counts.failed or counts.errors or not counts.total):
                            raise ValueError("pytest exit code disagrees with JUnit counts")
                finally:
                    box.cleanup()
                    if job.get("container_id"):
                        try:
                            client.containers.get(job["container_id"])
                        except NotFound:
                            job["container_removed"] = True
                    else:
                        job["container_removed"] = True
                    save(job_dir / "evidence.json", job)
                if not job["container_removed"]:
                    raise RuntimeError("sandbox cleanup was not confirmed")
            totals = TestCounts(**{key: sum(job["counts"][key] for job in record["jobs"])
                                  for key in ("passed", "failed", "errors", "skipped")})
            logs = "\n".join(f"[{job['name']}]\n{job['stdout']}\n{job['stderr']}" for job in record["jobs"])
            receipt = SandboxTestResult(code_digest=code_digest(code), execution_mode="docker", executed=True,
                                        run_id=run_id, exit_code=next((job["exit_code"] for job in record["jobs"] if job["exit_code"]), 0),
                                        counts=totals, logs=logs)
            record.update(status="COMPLETED", receipt=receipt.model_dump())
            return receipt
        except BaseException as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record["elapsed_seconds"] = time.monotonic() - started
            save(run_dir / "summary.json", record)
            if self.client is None:
                client.close()
