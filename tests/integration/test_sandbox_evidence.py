"""Assert real-daemon evidence inside a sandbox without exposing Docker's socket."""

import errno
import hashlib
import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def evidence():
    path = os.environ.get("SANDBOX_EVIDENCE")
    if not path:
        pytest.skip("use scripts/validate_sandbox.py to collect real Docker evidence")
    return json.loads(Path(path).read_text())


def test_real_execution_and_artifact(evidence):
    code, stdout, stderr = evidence["probe"]
    observed = json.loads(stdout)
    assert code == 7
    assert stderr == "probe-stderr\n"
    assert observed["input"] == "sandbox input\n"
    assert observed["workspace_readable"]
    assert observed["artifact"] == "/output/result.txt"
    assert observed["sha256"] == hashlib.sha256(b"sandbox artifact\n").hexdigest()
    assert evidence["artifact"][0] == 0
    assert evidence["artifact"][1] == "sandbox artifact\n"


def test_actual_isolation(evidence):
    observed = json.loads(evidence["probe"][1])
    assert observed["uid"] == 65532
    # Some Linux kernels expose inactive tunnel devices even with network=none.
    # Assert lack of routes and actual reachability, rather than interface names.
    assert observed["ipv4_routes"] == []
    assert observed["network_errno"] == errno.ENETUNREACH
    assert observed["docker_socket"] is False
    assert int(observed["capabilities"], 16) == 0
    assert observed["no_new_privileges"] == "1"
    assert observed["seccomp"] == "2"
    for result in observed["writes"].values():
        assert isinstance(result, dict)
        assert result["errno"] in (errno.EACCES, errno.EROFS)


def test_daemon_enforced_limits(evidence):
    config = evidence["host_config"]
    assert config["NanoCpus"] == 2_000_000_000
    assert config["Memory"] == 6 * 1024**3
    assert config["MemorySwap"] == 6 * 1024**3
    assert config["PidsLimit"] == 256
    assert config["NetworkMode"] == "none"
    assert config["ReadonlyRootfs"] is True
    assert config["Privileged"] is False
    assert config["CapDrop"] == ["ALL"]
    assert config["AutoRemove"] is True


def test_timeouts_kill_container_and_descendants(evidence):
    assert evidence["timeout"]["error"] == "SandboxTimeout"
    assert evidence["timeout"]["elapsed"] < 15
    assert evidence["timeout"]["removed"] is True


def test_output_flood_kills_container(evidence):
    assert evidence["flood"]["error"] == "SandboxOutputLimit"
    assert evidence["flood"]["removed"] is True


def test_success_and_failure_leave_no_probe_containers(evidence):
    assert evidence["normal_removed"] is True
    assert evidence["remaining_probe_containers"] == []
