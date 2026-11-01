"""Executed ONLY inside a Docker sandbox; emits observations, not assertions."""

import hashlib
import json
import os
from pathlib import Path
import socket
import sys


def attempt_write(path):
    try:
        Path(path).write_text("unexpected write", encoding="utf-8")
        return "writable"
    except OSError as error:
        return {"errno": error.errno, "type": type(error).__name__}


payload = b"sandbox artifact\n"
artifact = Path("/output/result.txt")
artifact.write_bytes(payload)
status = dict(
    line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line
)
with socket.socket() as connection:
    connection.settimeout(1)
    network_errno = connection.connect_ex(("192.0.2.1", 9))
print(json.dumps({
    "uid": os.getuid(),
    "input": Path("/input/fixture.txt").read_text(),
    "workspace_readable": Path("/workspace/pyproject.toml").is_file(),
    "writes": {path: attempt_write(path) for path in (
        "/workspace/forbidden.txt", "/input/forbidden.txt", "/etc/forbidden.txt",
    )},
    "interfaces": sorted(p.name for p in Path("/sys/class/net").iterdir()),
    "ipv4_routes": Path("/proc/net/route").read_text().splitlines()[1:],
    "network_errno": network_errno,
    "docker_socket": Path("/var/run/docker.sock").exists(),
    "capabilities": status["CapEff"].strip(),
    "no_new_privileges": status["NoNewPrivs"].strip(),
    "seccomp": status["Seccomp"].strip(),
    "sha256": hashlib.sha256(payload).hexdigest(),
    "artifact": str(artifact),
}))
print("probe-stderr", file=sys.stderr)
sys.exit(7)
