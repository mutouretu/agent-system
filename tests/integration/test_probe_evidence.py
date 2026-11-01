import json
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def evidence():
    directory = os.environ.get("PROBE_EVIDENCE_DIR")
    if not directory:
        pytest.skip("run scripts/validate_probe.py to collect probe evidence")
    root = Path(directory)
    return json.loads((root / "context.json").read_text()), json.loads((root / "benchmark.json").read_text()), root


def test_context_byte_and_token_upper_bounds(evidence):
    report, _, root = evidence
    size = (root / "context.json").stat().st_size
    assert size <= 64 * 1024
    assert size <= 16 * 1024
    assert size == report["budget"]["serialized_bytes"]
    assert size == report["budget"]["token_upper_bound"]
    assert report["budget"]["truncated"]
    assert report["status"] == "error"
    assert sum(log["bytes_seen"] for log in report["logs"].values()) > 1024 * 1024


def test_sampling_decay_and_repeated_log_summary(evidence):
    report, _, _ = evidence
    functions = {entry["function"]: entry for entry in report["sampling"]}
    for name in ("process_record", "normalize", "divide"):
        counts = functions[name]
        assert counts["calls"] == 10_000
        assert counts["sampled"] < 100
        assert counts["decimated"] > 9900
        assert all(counts["levels"][level] > 0 for level in ("dense", "stride16", "stride512"))
    logs = report["logs"]["stdout"]
    assert logs["events"] == 10_001
    assert logs["repeated"] == 9999
    assert logs["truncated_writes"] >= 1
    assert logs["oversized_examples"][0]["bytes"] > 1024 * 1024
    assert len(report["samples"]) <= 48
    assert report["samples_evicted"] > 0


def test_terminal_key_frames_survive_truncation(evidence):
    report, _, _ = evidence
    error = report["exception"]
    assert error["type"] == "ZeroDivisionError"
    assert "division by zero" in error["traceback"]
    assert [frame["function"] for frame in error["frames"]] == [
        "run_batch", "process_record", "normalize", "divide",
    ]
    last = error["frames"][-1]
    assert last["locals"]["iteration"] == 9999
    assert last["locals"]["record_id"] == "record-09999"
    assert last["locals"]["denominator"] == 0
    assert last["locals"]["numerator"] > 0
    assert last["locals"]["context_label"] == "normalizing-score"
    assert last["locals"]["payload"]["truncated"]
    assert "denominator" in last["arguments"]
    assert report["budget"]["exception_locals_trimmed"] == 0


def test_benchmark_semantics_and_alternating_pairs(evidence):
    _, benchmark, _ = evidence
    assert len(benchmark["pairs"]) == 7
    for index, pair in enumerate(benchmark["pairs"]):
        assert pair["order"] == ("probe-first" if index % 2 else "baseline-first")
        assert pair["probed"]["status"] == "ok"
        assert pair["baseline"]["result"] == pair["probed"]["result"]
        assert pair["baseline"]["bytes_seen"] == pair["probed"]["bytes_seen"]
        assert pair["probed"]["serialized_bytes"] <= 16 * 1024


def test_probe_overhead_within_predeclared_limits(evidence):
    _, benchmark, _ = evidence
    assert benchmark["policy"]["max_median_overhead_ratio"] == 1.0
    assert benchmark["policy"]["max_median_added_seconds"] == 0.150
    assert benchmark["median_overhead_ratio"] <= 1.0, benchmark
    assert benchmark["median_added_seconds"] <= 0.150, benchmark
