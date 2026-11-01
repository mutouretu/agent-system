"""Start the persistent dashboard in Docker; no generated code runs on the host.

Usage: python examples/web_dashboard.py [--port 8000] [--no-open]
"""
import argparse
import os
from pathlib import Path
import subprocess
import sys
import webbrowser


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65534:
        parser.error("port must be between 1024 and 65534")
    root = Path(__file__).resolve().parents[1]
    env = {**os.environ, "API_PORT": str(args.port), "PROBE_PORT": str(args.port + 1), "PYTHONDONTWRITEBYTECODE": "1"}
    result = subprocess.run(["bash", str(root / "scripts/deploy.sh"), "start"], cwd=root, env=env, check=False)
    if result.returncode:
        return result.returncode
    url = f"http://127.0.0.1:{args.port}/"
    print(f"Project List Dashboard: {url}")
    print("执行研发任务还需本地 Worker：./scripts/workbench.sh")
    if not args.no_open:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())
