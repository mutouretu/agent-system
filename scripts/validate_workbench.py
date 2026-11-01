"""Trusted evidence controller; executes fixed PostgreSQL probe only via Docker SDK."""

import argparse
import json
import os
from pathlib import Path
import uuid

import docker
import httpx

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", help="existing browser-triggered live run to archive")
    args = parser.parse_args()
    directory = ROOT / "sandbox-validation" / f"workbench-check-{uuid.uuid4().hex}"
    directory.mkdir(parents=True)
    config = directory / "docker-config"
    config.mkdir()
    os.environ["DOCKER_CONFIG"] = str(config)
    summary = {"status": "FAILED", "evidence_dir": str(directory)}
    client = docker.from_env(timeout=30)
    try:
        project = os.environ.get("RD_SERVICE_PROJECT", "rd-platform-service")
        candidates = client.containers.list(filters={"label": [f"com.docker.compose.project={project}", "com.docker.compose.service=api"]})
        if len(candidates) != 1:
            raise RuntimeError("one API service must be running")
        container = candidates[0]
        script = (ROOT / "tests/probes/workbench_postgres.py").read_bytes()
        # Send this fixed probe as data to Docker exec. Python compilation and
        # assertions happen inside Linux, with the service rootfs still read-only.
        code, streams = container.exec_run(["python", "-c", script.decode()], demux=True)
        stdout, stderr = ((value or b"").decode() for value in streams)
        (directory / "postgres-probe.json").write_text(json.dumps({"exit_code": code, "stdout": stdout, "stderr": stderr}, indent=2))
        if code:
            raise RuntimeError("PostgreSQL probe failed; inspect postgres-probe.json")
        summary["postgres"] = json.loads(stdout)
        api = "http://127.0.0.1:" + os.environ.get("API_PORT", "8000")
        with httpx.Client(base_url=api, trust_env=False, timeout=10) as http:
            response = http.get("/")
            response.raise_for_status()
            summary["frontend_served"] = "app.js" in response.text
            response = http.get("/v1/workbench")
            response.raise_for_status()
            summary["workbench"] = response.json()
            if args.run_id:
                response = http.get(f"/v1/runs/{args.run_id}")
                response.raise_for_status()
                run = response.json()
                (directory / "live-run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2))
                if run["status"] != "COMPLETED" or run["summary"].get("execution_mode") != "docker":
                    raise RuntimeError("live run is not a completed Docker execution")
                summary["live_run"] = {"id": run["id"], "status": run["status"], "summary": run["summary"]}
        summary["status"] = "PASS"
        return 0
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        client.close()
        (directory / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print(json.dumps({key: value for key, value in summary.items() if key != "live_run"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
