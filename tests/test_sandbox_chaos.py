"""Run this file with the control-plane Python; all probes and pytest run in Docker.

PYTHONDONTWRITEBYTECODE=1 .venv-control/bin/python tests/test_sandbox_chaos.py
Requires a local python:3.12-slim image, cgroup v2, and offline pytest wheels.
"""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
import shutil
import sys
import time
import uuid


SYSTEM_PROBE = r'''
import json
from pathlib import Path
import sys

def read_result(path):
    try:
        with open(path, "rb") as stream:
            stream.read(128)
        return {"readable": True}
    except OSError as error:
        return {"readable": False, "errno": error.errno}

def write_result(path):
    try:
        Path(path).write_text("sandbox isolation probe", encoding="utf-8")
        return {"writable": True}
    except OSError as error:
        return {"writable": False, "errno": error.errno}

# /etc/passwd belongs to the container; use a synthetic unmounted host sentinel.
print(json.dumps({
    "container_passwd": read_result("/etc/passwd"),
    "host_sentinel": read_result(sys.argv[1]),
    "ssh_private_key": read_result(str(Path.home() / ".ssh" / "id_rsa")),
    "root_private_key": read_result("/root/.ssh/id_rsa"),
    "writes": {path: write_result(path) for path in (
        "/workspace/forbidden.txt", "/input/forbidden.txt",
        "/etc/sandbox-forbidden.txt", "/sandbox-forbidden.txt",
        "/tmp/allowed.txt", "/output/allowed.txt",
    )},
}))
'''

NETWORK_PROBE = r'''
import json
import socket
import urllib.request

with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as connection:
    connection.settimeout(2)
    try:
        tcp_errno = connection.connect_ex(("8.8.8.8", 443))
    except OSError as error:
        tcp_errno = error.errno

https = {"succeeded": False}
try:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open("https://httpbin.org/get", timeout=2) as response:
        https = {"succeeded": True, "status": response.status}
except Exception as error:
    https.update({"error_type": type(error).__name__, "message": str(error)})
print(json.dumps({"tcp_errno": tcp_errno, "https": https}))
'''

MEMORY_EVENTS_PROBE = r'''
import json
from pathlib import Path

root = Path("/sys/fs/cgroup")
events = {key: int(value) for key, value in (
    line.split() for line in (root / "memory.events").read_text().splitlines()
)}
print(json.dumps({
    "events": events,
    "memory_max": (root / "memory.max").read_text().strip(),
}))
'''

OOM_PROBE = r'''
from pathlib import Path

# Prefer killing this allocator, preserving the container for OOM inspection.
Path("/proc/self/oom_score_adj").write_text("1000")
blocks = []
while True:
    block = bytearray(1024 * 1024)
    for offset in range(0, len(block), 4096):
        block[offset] = 1
    blocks.append(block)
'''

CPU_PROBE = "while True:\n    pass\n"

PROCESS_PROBE = r'''
import json
from pathlib import Path
import subprocess

# Subprocesses are allowed inside the container; the limit must bound them.
shell = subprocess.run(
    ["/bin/sh", "-c", "id"], capture_output=True, text=True, timeout=3,
)
children = []
blocked_errno = None
try:
    for _ in range(512):
        children.append(subprocess.Popen(
            ["/bin/sleep", "120"], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        ))
except OSError as error:
    blocked_errno = error.errno
root = Path("/sys/fs/cgroup")
print(json.dumps({
    "shell_exit_code": shell.returncode,
    "shell_stdout": shell.stdout,
    "spawned": len(children),
    "blocked_errno": blocked_errno,
    "pids_current": int((root / "pids.current").read_text()),
    "pids_max": (root / "pids.max").read_text().strip(),
}), flush=True)
# Deliberately leave detached children for whole-container cleanup.
'''

DEPENDENCY_PROBE = "import sympy\nassert sympy.Integer(2) + sympy.Integer(3) == 5\n"


def _command_json(record, command="probe"):
    result = record["commands"][command]
    assert result["exit_code"] == 0, result
    return json.loads(result["stdout"])


def _assert_cleaned(record):
    assert record["removed"] is True, record
    assert record["container_id"]


# The trusted host controller does not import or run pytest.
if __name__ != "__main__":
    import pytest

    pytestmark = pytest.mark.integration

    @pytest.fixture(scope="session")
    def evidence():
        path = os.environ.get("SANDBOX_CHAOS_EVIDENCE")
        if not path:
            pytest.skip("run tests/test_sandbox_chaos.py to collect real chaos evidence")
        if sys.platform != "linux" or not Path("/.dockerenv").exists():
            pytest.fail("pytest assertions must run inside the validation container")
        return json.loads(Path(path).read_text(encoding="utf-8"))


def test_system_access(evidence):
    """Unmounted host data and SSH keys are inaccessible; rootfs rejects writes."""
    record = evidence["system"]
    assert record["error"] is None, record
    observed = _command_json(record)
    assert observed["container_passwd"]["readable"] is True
    for name in ("host_sentinel", "ssh_private_key", "root_private_key"):
        result = observed[name]
        assert result["readable"] is False, (name, result)
        assert result["errno"] in (errno.ENOENT, errno.EACCES, errno.EPERM)
    for path in (
        "/workspace/forbidden.txt", "/input/forbidden.txt",
        "/etc/sandbox-forbidden.txt", "/sandbox-forbidden.txt",
    ):
        result = observed["writes"][path]
        assert result["writable"] is False, (path, result)
        assert result["errno"] in (errno.EROFS, errno.EACCES, errno.EPERM)
    # These two paths are explicitly writable by design.
    assert observed["writes"]["/tmp/allowed.txt"]["writable"] is True
    assert observed["writes"]["/output/allowed.txt"]["writable"] is True
    _assert_cleaned(record)


def test_network_egress(evidence):
    """Verify direct-IP ENETUNREACH, not merely a DNS or certificate failure."""
    record = evidence["network"]
    assert record["error"] is None, record
    assert record["limits"]["NetworkMode"] == "none"
    observed = _command_json(record)
    assert observed["tcp_errno"] == errno.ENETUNREACH, observed
    assert observed["https"]["succeeded"] is False, observed
    assert observed["https"]["error_type"], observed
    _assert_cleaned(record)


def test_resource_exhaustion(evidence):
    """Require kernel OOM evidence and SIGKILL, plus an independent CPU timeout."""
    memory = evidence["memory"]
    assert memory["error"] is None, memory
    assert memory["limits"]["Memory"] == 128 * 1024**2
    assert memory["limits"]["MemorySwap"] == 128 * 1024**2
    before = _command_json(memory, "before")
    after = _command_json(memory, "after")
    assert int(before["memory_max"]) == 128 * 1024**2
    assert memory["commands"]["allocate"]["exit_code"] == 137, memory
    assert after["events"]["oom_kill"] > before["events"]["oom_kill"]
    _assert_cleaned(memory)
    cpu = evidence["cpu"]
    assert cpu["limits"]["NanoCpus"] == 2_000_000_000
    assert cpu["error"]["type"] == "SandboxTimeout", cpu
    assert 0.8 <= cpu["elapsed_seconds"] < 15, cpu
    _assert_cleaned(cpu)


def test_process_spawning(evidence):
    """PID exhaustion returns EAGAIN; container removal reaps detached children."""
    record = evidence["process"]
    assert record["error"] is None, record
    assert record["limits"]["PidsLimit"] == 256
    observed = _command_json(record)
    assert observed["shell_exit_code"] == 0, observed
    assert "uid=65532" in observed["shell_stdout"]
    assert 0 < observed["spawned"] < 256
    assert observed["blocked_errno"] == errno.EAGAIN, observed
    assert int(observed["pids_max"]) == 256
    assert observed["pids_current"] <= 256
    _assert_cleaned(record)


def test_missing_dependency(evidence):
    """Retain the real traceback; missing offline dependencies cannot be invented."""
    record = evidence["dependency"]
    assert record["error"] is None, record
    assert not evidence["sympy_wheels"], evidence["sympy_wheels"]
    result = record["commands"]["probe"]
    assert result["exit_code"] != 0
    assert "Traceback (most recent call last):" in result["stderr"]
    assert "ModuleNotFoundError: No module named 'sympy'" in result["stderr"]
    assert result["stdout"] == ""
    health = record["commands"]["health"]
    assert health["exit_code"] == 0
    assert health["stdout"].strip() == "runtime-alive"
    _assert_cleaned(record)


def _find_project_root():
    for candidate in Path(__file__).resolve().parents:
        if (candidate / "src/rd_platform/runtime/sandbox.py").is_file():
            return candidate
    raise RuntimeError("Cannot find src/rd_platform/runtime/sandbox.py")


def _run_controller():
    """Host-side lifecycle management only; all dangerous code is sent to Docker."""
    root = _find_project_root()
    state = root / "sandbox-validation"
    wheels = state / "wheels"
    if not wheels.is_dir():
        raise RuntimeError(f"Missing offline wheels: {wheels}")
    run_dir = state / f"chaos-{uuid.uuid4().hex}"
    config_dir = run_dir / "docker-config"
    config_dir.mkdir(parents=True)
    os.environ["DOCKER_CONFIG"] = str(config_dir)
    sys.path.insert(0, str(root / "src"))

    import docker
    from docker.errors import DockerException, NotFound
    from rd_platform.runtime.sandbox import DockerSandbox, SandboxError

    workspace = run_dir / "workspace"
    inputs = run_dir / "input"
    workspace.mkdir()
    inputs.mkdir()
    shutil.copy2(__file__, workspace / "test_sandbox_chaos.py")
    (workspace / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    integration: real Docker evidence assertions\n",
        encoding="utf-8",
    )
    shutil.copytree(wheels, inputs / "wheels")
    sentinel = run_dir / "host-only-sentinel.txt"
    sentinel.write_text("synthetic-host-only-marker", encoding="utf-8")
    evidence = {"sympy_wheels": [
        path.name for path in wheels.iterdir()
        if path.name.lower().startswith("sympy-") and path.suffix.lower() == ".whl"
    ]}
    boxes = []
    image = os.environ.get("SANDBOX_IMAGE", "python:3.12-slim")
    client = docker.from_env(timeout=10)

    def removed(container_id):
        try:
            client.containers.get(container_id)
        except NotFound:
            return True
        return False

    def new_box(name, timeout=15):
        output = run_dir / f"output-{name}"
        output.mkdir()
        # Only this new task output directory is made writable, not system paths.
        output.chmod(0o777)
        box = DockerSandbox(
            image, workspace, inputs, output, client=client,
            timeout_seconds=timeout, max_output_bytes=1024 * 1024,
        )
        boxes.append(box)
        return box

    def collect(name, commands, *, timeout=15, memory_limit=None):
        print(f"Collecting isolated scenario: {name}", flush=True)
        box = new_box(name, timeout)
        record = {"commands": {}, "error": None, "container_id": None, "removed": False}
        started = None
        try:
            box.start()
            record["container_id"] = box.container_id
            container = client.containers.get(box.container_id)
            if memory_limit is not None:
                # Tighten the real cgroup before allocation; no RLIMIT_AS substitute.
                container.update(mem_limit=memory_limit, memswap_limit=memory_limit)
                container.reload()
            record["image_id"] = container.attrs["Image"]
            record["limits"] = {key: container.attrs["HostConfig"][key] for key in (
                "Memory", "MemorySwap", "NanoCpus", "PidsLimit", "NetworkMode",
            )}
            started = time.monotonic()
            for label, code, arguments in commands:
                exit_code, stdout, stderr = box.exec_run(["python", "-c", code, *arguments])
                record["commands"][label] = {
                    "exit_code": exit_code, "stdout": stdout, "stderr": stderr,
                }
        except (SandboxError, DockerException) as error:
            record["error"] = {"type": type(error).__name__, "message": str(error)}
        finally:
            box.cleanup()
            if started is not None:
                record["elapsed_seconds"] = time.monotonic() - started
            if record["container_id"] is not None:
                record["removed"] = removed(record["container_id"])
        evidence[name] = record
        (inputs / "evidence.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")

    try:
        client.ping()
        client.images.get(image)
        collect("system", [("probe", SYSTEM_PROBE, [str(sentinel)])])
        collect("network", [("probe", NETWORK_PROBE, [])])
        collect("memory", [
            ("before", MEMORY_EVENTS_PROBE, []),
            ("allocate", OOM_PROBE, []),
            ("after", MEMORY_EVENTS_PROBE, []),
        ], memory_limit="128m")
        collect("cpu", [("probe", CPU_PROBE, [])], timeout=1)
        collect("process", [("probe", PROCESS_PROBE, [])], timeout=20)
        collect("dependency", [
            ("probe", DEPENDENCY_PROBE, []),
            ("health", "print('runtime-alive')", []),
        ])
        print("Running pytest inside isolated validation container", flush=True)
        with new_box("pytest", timeout=120) as box:
            exit_code, stdout, stderr = box.exec_run([
                "python", "-m", "pip", "install", "--no-index", "--no-compile",
                "--disable-pip-version-check", "--find-links=/input/wheels",
                "--target=/tmp/test-deps", "pytest==8.3.5",
            ])
            if exit_code:
                print(stdout, end="", flush=True)
                print(stderr, end="", file=sys.stderr, flush=True)
                return exit_code
            exit_code, stdout, stderr = box.exec_run([
                "/usr/bin/env", "PYTHONPATH=/tmp/test-deps",
                "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                "SANDBOX_CHAOS_EVIDENCE=/input/evidence.json",
                "python", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "--junitxml=/output/pytest.xml", "/workspace/test_sandbox_chaos.py",
            ])
            (run_dir / "pytest-stdout.txt").write_text(stdout, encoding="utf-8")
            (run_dir / "pytest-stderr.txt").write_text(stderr, encoding="utf-8")
            print(stdout, end="", flush=True)
            print(stderr, end="", file=sys.stderr, flush=True)
        print(f"Evidence and reports: {run_dir}", flush=True)
        return exit_code
    finally:
        failures = []
        receipts = []
        for box in reversed(boxes):
            try:
                box.cleanup()
                receipts.append({"name": box.name, "removed": removed(box.name)})
                if not receipts[-1]["removed"]:
                    failures.append(f"{box.name}: still exists after cleanup")
            except Exception as error:
                failures.append(f"{box.name}: {error}")
        (run_dir / "cleanup.json").write_text(json.dumps(receipts, indent=2), encoding="utf-8")
        client.close()
        if failures:
            raise RuntimeError("Container cleanup failed: " + "; ".join(failures))


if __name__ == "__main__":
    raise SystemExit(_run_controller())
