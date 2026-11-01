"""Docker healthcheck entry point; runs inside the service container."""

import json
import sys
from urllib.request import ProxyHandler, build_opener


def main():
    try:
        with build_opener(ProxyHandler({})).open(sys.argv[1], timeout=2) as response:
            ready = response.status == 200 and json.load(response).get("status") == "ok"
        return 0 if ready else 1
    except Exception as error:
        print(f"healthcheck failed: {type(error).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
