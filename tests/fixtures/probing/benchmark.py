"""Executed only in the Docker sandbox, independently of pytest instrumentation."""

from contextlib import redirect_stdout, redirect_stderr
import json
from pathlib import Path
import statistics
import time

from rd_platform.runtime.probe import CountingSink, RuntimeProbe, encoded
from workload import FUNCTIONS, run_batch


POLICY = {
    "pairs": 7,
    "warmup_pairs": 1,
    "iterations_per_run": 10_000,
    "max_median_overhead_ratio": 1.0,
    "max_median_added_seconds": 0.150,
    "max_output_bytes": 64 * 1024,
    "max_context_token_upper_bound": 16 * 1024,
}


def main():
    output = Path("/output")
    output.joinpath("policy.json").write_bytes(encoded(POLICY))
    payload = {"text": "大对象内容" * 100_000, "items": list(range(5000))}
    context = RuntimeProbe(FUNCTIONS).run(run_batch, payload, fail=True)
    context_bytes = encoded(context) + b"\n"
    output.joinpath("context.json").write_bytes(context_bytes)

    def baseline():
        stdout, stderr = CountingSink(), CountingSink()
        started = time.perf_counter()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = run_batch(payload)
        elapsed = time.perf_counter() - started
        return {"seconds": elapsed, "result": result, "bytes_seen": stdout.bytes_seen + stderr.bytes_seen}

    def instrumented():
        started = time.perf_counter()
        report = RuntimeProbe(FUNCTIONS).run(run_batch, payload)
        serialized = encoded(report) + b"\n"
        elapsed = time.perf_counter() - started
        return {
            "seconds": elapsed, "result": report["result"], "status": report["status"],
            "serialized_bytes": len(serialized),
            "bytes_seen": sum(log["bytes_seen"] for log in report["logs"].values()),
        }

    baseline()
    instrumented()
    pairs = []
    for index in range(POLICY["pairs"]):
        if index % 2:
            probed, bare = instrumented(), baseline()
        else:
            bare, probed = baseline(), instrumented()
        pairs.append({"order": "probe-first" if index % 2 else "baseline-first", "baseline": bare, "probed": probed})
    base_median = statistics.median(pair["baseline"]["seconds"] for pair in pairs)
    probe_median = statistics.median(pair["probed"]["seconds"] for pair in pairs)
    benchmark = {
        "policy": POLICY,
        "pairs": pairs,
        "baseline_median_seconds": base_median,
        "probed_median_seconds": probe_median,
        "median_overhead_ratio": probe_median / base_median - 1,
        "median_added_seconds": probe_median - base_median,
        "baseline_rows_per_second": POLICY["iterations_per_run"] / base_median,
        "probed_rows_per_second": POLICY["iterations_per_run"] / probe_median,
        "measurement_scope": "same-process alternating pairs; includes capture, snapshots, budget fitting and serialization; excludes container startup and pip",
    }
    output.joinpath("benchmark.json").write_bytes(encoded(benchmark))
    # This is the entire exec output; the Docker SDK independently enforces 64 KiB.
    print(context_bytes.decode("utf-8"), end="")


if __name__ == "__main__":
    main()
