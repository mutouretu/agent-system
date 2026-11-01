import json
import sys

import pytest

from rd_platform.runtime.probe import ProbeLimits, RuntimeProbe, encoded, preview, sample_level


def test_sampling_schedule_reaches_all_levels():
    assert [sample_level(n) for n in (1, 8, 9, 16, 256, 257, 512, 9999)] == [
        "dense", "dense", None, "stride16", "stride16", None, "stride512", None,
    ]


def test_preview_avoids_custom_code_and_cycles():
    class Hostile:
        def __repr__(self):
            raise AssertionError("custom repr must not run")

    cyclic = []
    cyclic.append(cyclic)
    assert preview(Hostile()) == {"type": "Hostile", "omitted": True}
    assert len(encoded(preview(cyclic))) < 1024
    assert preview(1 << 10000)["truncated"]
    assert preview(float("nan"))["nonfinite"]


def test_error_preserves_arguments_locals_and_profile_state():
    def fail(numerator, denominator):
        detail = "critical"
        return numerator / denominator

    previous = sys.getprofile()
    stdout, stderr = sys.stdout, sys.stderr
    report = RuntimeProbe([fail]).run(fail, 9, 0)
    assert sys.getprofile() is previous
    assert (sys.stdout, sys.stderr) == (stdout, stderr)
    assert report["status"] == "error"
    assert report["exception"]["type"] == "ZeroDivisionError"
    frame = report["exception"]["frames"][-1]
    assert frame["locals"] == {"numerator": 9, "denominator": 0, "detail": "critical"}
    assert frame["arguments"] == ["numerator", "denominator"]


def test_custom_exception_str_is_not_called():
    class UnsafeError(Exception):
        @property
        def args(self):
            raise AssertionError("custom exception property must not run")

        def __str__(self):
            raise AssertionError("custom exception formatting must not run")

    def fail():
        raise UnsafeError("preserved")

    report = RuntimeProbe([fail]).run(fail)
    assert report["exception"]["message"] == "preserved"


def test_monitoring_slots_and_local_events_are_released():
    if not hasattr(sys, "monitoring"):
        pytest.skip("sys.monitoring requires Python 3.12+")

    def succeed():
        return 1

    before = {slot: sys.monitoring.get_tool(slot) for slot in (3, 4)}
    report = RuntimeProbe([succeed]).run(succeed)
    assert {slot: sys.monitoring.get_tool(slot) for slot in (3, 4)} == before
    for slot, owner in before.items():
        if owner is None:
            assert sys.monitoring.get_local_events(slot, succeed.__code__) == 0
    assert report["sampling"][0]["calls"] == 1


def test_profile_fallback_when_monitoring_unavailable(monkeypatch):
    monkeypatch.delattr(sys, "monitoring", raising=False)

    def succeed(value):
        return value

    previous = sys.getprofile()
    report = RuntimeProbe([succeed]).run(succeed, 7)
    assert report["backend"].startswith("sys.setprofile")
    assert report["sampling"][0]["calls"] == 1
    assert report["result"] == 7
    assert sys.getprofile() is previous


def test_interrupt_restores_streams_and_profile():
    def interrupt():
        raise KeyboardInterrupt()

    previous = sys.getprofile()
    streams = sys.stdout, sys.stderr
    with pytest.raises(KeyboardInterrupt):
        RuntimeProbe([interrupt]).run(interrupt)
    assert sys.getprofile() is previous
    assert (sys.stdout, sys.stderr) == streams


@pytest.mark.parametrize("budget", [4096, 8192, 16384])
def test_unicode_and_large_logs_fit_budget(budget):
    def write():
        for index in range(1000):
            print("测试" * 1000)
        raise ValueError("错误" * 200)

    report = RuntimeProbe([write], ProbeLimits(max_context_tokens=budget)).run(write)
    serialized = encoded(report) + b"\n"
    assert len(serialized) <= budget
    assert report["budget"]["serialized_bytes"] == len(serialized)
    assert report["budget"]["token_upper_bound"] == len(serialized)
    assert json.loads(serialized)["exception"]["type"] == "ValueError"
    assert report["budget"]["truncated"]


def test_probe_single_use_and_success_result():
    def succeed():
        return 42

    probe = RuntimeProbe([succeed])
    assert probe.run(succeed)["result"] == 42
    with pytest.raises(RuntimeError, match="single-use"):
        probe.run(succeed)


def test_invalid_budget_rejected():
    with pytest.raises(ValueError):
        ProbeLimits(max_context_tokens=200)
    with pytest.raises(ValueError):
        ProbeLimits(max_samples=0)
