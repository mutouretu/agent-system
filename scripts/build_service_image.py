"""Trusted host controller: download wheels, then build only via Docker SDK."""

import argparse
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]
CACHE = ROOT / "sandbox-validation/service"


def check_ports(client):
    ports = [int(os.environ.get(name, default)) for name, default in (("API_PORT", "8000"), ("PROBE_PORT", "8001"))]
    if len(set(ports)) != 2 or any(not 1024 <= port <= 65535 for port in ports):
        raise ValueError("API_PORT and PROBE_PORT must be distinct integers between 1024 and 65535")
    project = os.environ.get("RD_SERVICE_PROJECT", "rd-platform-service")
    owned = set()
    for container in client.containers.list(filters={"label": f"com.docker.compose.project={project}"}):
        for bindings in container.attrs["NetworkSettings"]["Ports"].values():
            for binding in bindings or []:
                owned.add(int(binding["HostPort"]))
    for port in ports:
        if port in owned:
            continue
        with socket.socket() as listener:
            # A recently stopped Compose service can leave TIME_WAIT sockets behind.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"Port {port} is occupied; set API_PORT/PROBE_PORT to unused ports") from exc


def build(client):
    from docker.errors import ImageNotFound

    try:
        client.images.get("postgres:17")
    except ImageNotFound:
        print("Provisioning PostgreSQL 17 via Docker SDK", flush=True)
        client.images.pull("postgres:17")

    base = "python:3.12-slim"
    try:
        image = client.images.get(base)
    except ImageNotFound:
        image = client.images.pull(base)
    architecture = {"arm64": "aarch64", "amd64": "x86_64"}.get(image.attrs["Architecture"])
    if not architecture or image.attrs["Os"] != "linux":
        raise RuntimeError("Service requires a Linux arm64/amd64 Python 3.12 image")
    wheels = CACHE / f"wheels-{architecture}"
    wheels.mkdir(parents=True, exist_ok=True)
    # Binary wheels only: no host compilation, installation or project execution.
    subprocess.run([
        sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--only-binary=:all:",
        "--platform", f"manylinux2014_{architecture}", "--platform", f"manylinux_2_28_{architecture}",
        "--python-version", "3.12", "--implementation", "cp", "--abi", "cp312",
        "--dest", str(wheels), "-r", str(ROOT / "requirements-service.txt"),
    ], check=True, timeout=180)
    tag = os.environ.get("RD_SERVICE_IMAGE", "rd-platform-service:0.3.0-dev")
    with tempfile.TemporaryDirectory(prefix="build-", dir=CACHE) as temporary:
        context = Path(temporary)
        for name in ("Dockerfile.service", "requirements-service.txt"):
            shutil.copy2(ROOT / name, context / name)
        for name in ("src", "examples"):
            shutil.copytree(ROOT / name, context / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        shutil.copytree(wheels, context / "wheels")
        built, records = client.images.build(
            path=str(context), dockerfile="Dockerfile.service", tag=tag,
            network_mode="none", rm=True, forcerm=True, pull=False,
        )
        for record in records:
            if record.get("stream"):
                print(record["stream"], end="", flush=True)
    (CACHE / "build.json").write_text(json.dumps({
        "image": tag, "image_id": built.id, "base_image_id": image.id,
        "architecture": architecture, "build_network": "none",
    }, indent=2) + "\n")
    print(f"Built {tag}: {built.id}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-ports", action="store_true", help="only check daemon and published ports")
    mode.add_argument("--check-health", action="store_true", help="check health via host published ports")
    args = parser.parse_args()
    config = CACHE / "docker-config"
    config.mkdir(parents=True, exist_ok=True)
    os.environ["DOCKER_CONFIG"] = str(config)
    import docker
    from docker.errors import BuildError

    client = docker.from_env(timeout=180)
    try:
        try:
            client.ping()
            if client.info()["OSType"] != "linux":
                raise RuntimeError("Docker must run Linux containers")
            if args.check_ports:
                check_ports(client)
            elif args.check_health:
                opener = build_opener(ProxyHandler({}))
                for name, default in (("API_PORT", "8000"), ("PROBE_PORT", "8001")):
                    port = int(os.environ.get(name, default))
                    with opener.open(f"http://127.0.0.1:{port}/health", timeout=5) as response:
                        if json.load(response).get("status") != "ok":
                            raise RuntimeError(f"Health check failed on port {port}")
                    print(f"Published port {port}: healthy")
            else:
                build(client)
        finally:
            client.close()
    except BuildError as exc:
        for line in exc.build_log:
            print(line.get("stream", line.get("error", "")), end="", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"Service preparation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
