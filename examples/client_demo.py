"""Standard-library HTTP client. No model credentials or third-party packages."""

import argparse
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener


def request(base, path, payload=None, method="GET"):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    call = Request(base.rstrip("/") + path, data=data, method=method, headers={"Content-Type": "application/json"})
    with build_opener(ProxyHandler({})).open(call, timeout=5) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--probe-url", default="http://127.0.0.1:8001")
    args = parser.parse_args()
    try:
        api = request(args.api_url, "/health")
        probe = request(args.probe_url, "/health")
        if api.get("status") != "ok" or probe.get("runtime_probe") != "ok":
            raise ValueError("services are not ready")
        payload = {"records": [{"id": "alpha", "score": 8}, {"id": "beta", "score": 3}, {"id": "alpha", "score": 9}]}
        result = request(args.api_url, "/v1/algorithms/filter", {**payload, "min_score": 5}, "POST")
        if result["records"] != [{"id": "alpha", "score": 9}]:
            raise ValueError("unexpected algorithm response")
        stored = request(args.api_url, "/v1/data/demo", payload, "PUT")
        dataset = request(args.api_url, "/v1/data/demo")
        if stored["count"] != 3 or dataset["count"] != 3:
            raise ValueError("dataset round trip failed")
        print(json.dumps({"status": "PASS", "api": api, "probe": probe, "algorithm": result, "data": stored,
                          "models": request(args.api_url, "/v1/models")}, ensure_ascii=False, indent=2))
        return 0
    except (HTTPError, URLError, TimeoutError, ValueError, KeyError) as error:
        print(f"Service verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
