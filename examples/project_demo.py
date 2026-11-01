"""Run a two-agent DAG through the local service using only the standard library."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError

from client_demo import request


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    try:
        agents = request(args.api_url, "/v1/agents")
        project = request(args.api_url, "/v1/projects", {
            "name": "Data to statistics", "context": {"source": "project_demo"},
            "tasks": [
                {"id": "prepare", "name": "Sort and deduplicate", "capability": "data_processing",
                 "input": {"values": [3, 1, 3, 2]}},
                {"id": "stats", "name": "Calculate statistics", "capability": "algorithm_execution",
                 "depends_on": ["prepare"]},
            ],
        }, "POST")
        result = request(args.api_url, f"/v1/projects/{project['id']}/dispatch", method="POST")
        if any(task["status"] != "COMPLETED" for task in result["tasks"]):
            raise ValueError(json.dumps(result, ensure_ascii=False))
        if result["tasks"][1]["output"]["mean"] != 2:
            raise ValueError("unexpected downstream output")
        print(json.dumps({"status": "PASS", "agents": agents["agents"], "project": result}, ensure_ascii=False, indent=2))
        return 0
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as error:
        print(f"Project verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
