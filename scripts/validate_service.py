"""Docker-only pytest and live Compose acceptance; run deploy.sh start first."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[1]


def main():
    run = ROOT / "sandbox-validation" / f"service-{uuid.uuid4().hex}"
    workspace, inputs, output, config = (run / name for name in ("workspace", "input", "output", "docker-config"))
    for directory in (workspace, inputs, output, config):
        directory.mkdir(parents=True)
    output.chmod(0o777)
    os.environ["DOCKER_CONFIG"] = str(config)
    sys.path.insert(0, str(ROOT / "src"))
    import docker
    from docker.errors import NotFound
    from rd_platform.runtime.sandbox import DockerSandbox

    def save(name, value):
        (run / name).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")

    for name in ("src", "tests", "scripts", "examples"):
        shutil.copytree(ROOT / name, workspace / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(ROOT / "pyproject.toml", workspace / "pyproject.toml")
    wheels = inputs / "wheels"
    wheels.mkdir()
    subprocess.run([
        sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--only-binary=:all:",
        "--platform", "any", "--python-version", "3.12", "--implementation", "cp", "--abi", "none",
        "--dest", str(wheels), "pytest==8.3.5", "docker==7.1.0", "httpx==0.28.1",
    ], check=True, timeout=180)
    client = docker.from_env(timeout=30)
    box = DockerSandbox(os.environ.get("RD_SERVICE_IMAGE", "rd-platform-service:0.3.0-dev"),
                        workspace, inputs, output, client=client, timeout_seconds=120)
    summary = {"status": "FAILED", "evidence_dir": str(run), "container_removed": False}
    print(f"Evidence directory: {run}", flush=True)
    try:
        with box:
            summary["test_container_id"] = box.container_id
            for label, command in (
                ("shell-syntax", ["bash", "-n", "/workspace/scripts/deploy.sh"]),
                ("dependencies", ["python", "-m", "pip", "install", "--no-index", "--no-compile",
                                  "--disable-pip-version-check", "--find-links=/input/wheels", "--target=/tmp/deps",
                                  "pytest==8.3.5", "docker==7.1.0", "httpx==0.28.1"]),
                ("pytest", ["/usr/bin/env", "PYTHONPATH=/tmp/deps:/workspace/src:/workspace", "PYTEST_DISABLE_PLUGIN_AUTOLOAD=1",
                            "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "--junitxml=/output/pytest.xml",
                            "/workspace/tests"]),
            ):
                code, stdout, stderr = box.exec_run(command)
                save(f"{label}.json", {"exit_code": code, "stdout": stdout, "stderr": stderr})
                print(stdout, end="", flush=True)
                if code:
                    raise RuntimeError(f"{label} failed: {stderr}")
        project = os.environ.get("RD_SERVICE_PROJECT", "rd-platform-service")
        containers = client.containers.list(filters={"label": f"com.docker.compose.project={project}"})
        services = {container.labels["com.docker.compose.service"]: container for container in containers}
        if set(services) != {"api", "probe", "db"}:
            raise RuntimeError("Start the api/probe/db Compose services before validation")
        summary["services"] = {}
        for name, container in services.items():
            container.reload()
            state, host = container.attrs["State"], container.attrs["HostConfig"]
            if name == "db":
                if state["Health"]["Status"] != "healthy":
                    raise RuntimeError("PostgreSQL is not healthy")
                summary["services"][name] = {"id": container.id, "health": "healthy", "image_id": container.attrs["Image"]}
                continue
            if state["Health"]["Status"] != "healthy" or not host["ReadonlyRootfs"]:
                raise RuntimeError(f"Unhealthy or writable-root service: {name}")
            if any(mount["Destination"] == "/var/run/docker.sock" for mount in container.attrs["Mounts"]):
                raise RuntimeError("Docker socket must not be exposed to services")
            if name == "api":
                mounts = {mount["Destination"]: mount for mount in container.attrs["Mounts"]}
                if mounts["/app/models"]["RW"] or not all(mounts[path]["RW"] for path in ("/app/data", "/app/logs")):
                    raise RuntimeError("Unexpected data/log/model mount modes")
            summary["services"][name] = {"id": container.id, "health": state["Health"]["Status"],
                                          "read_only_root": host["ReadonlyRootfs"], "image_id": container.attrs["Image"]}
            (run / f"{name}.log").write_bytes(container.logs(tail=100))
        for label, command in (
            ("client", ["python", "/app/examples/client_demo.py", "--probe-url", "http://probe:8001"]),
            ("project-client", ["python", "/app/examples/project_demo.py"]),
        ):
            code, streams = services["api"].exec_run(command, demux=True)
            stdout, stderr = (stream.decode("utf-8", errors="replace") if stream else "" for stream in streams)
            save(f"{label}.json", {"exit_code": code, "stdout": stdout, "stderr": stderr})
            if code:
                raise RuntimeError(f"{label} demo failed: {stderr}")
            summary[label] = json.loads(stdout)
        # The host controller only receives structured health evidence.
        opener = build_opener(ProxyHandler({}))
        summary["published_health"] = {}
        for name, port in (("api", os.environ.get("API_PORT", "8000")), ("probe", os.environ.get("PROBE_PORT", "8001"))):
            with opener.open(f"http://127.0.0.1:{port}/health", timeout=5) as response:
                summary["published_health"][name] = json.load(response)
            if summary["published_health"][name]["status"] != "ok":
                raise RuntimeError(f"Published {name} health failed")
        summary["status"] = "PASS"
        return 0
    except Exception as exc:
        summary["error"] = str(exc)
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        box.cleanup()
        if box.container_id:
            try:
                client.containers.get(box.container_id)
            except NotFound:
                summary["container_removed"] = True
        else:
            summary["container_removed"] = True
        save("summary.json", summary)
        client.close()
        print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    raise SystemExit(main())
