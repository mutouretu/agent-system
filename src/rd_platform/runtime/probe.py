"""Bounded, cooperative Python probes. Import and execute only in the sandbox.

This is observability, not an additional security boundary. Native fd writes,
subprocesses and other threads remain subject to DockerSandbox's hard limits.
No arbitrary repr/property is invoked when capturing object snapshots.
"""

from __future__ import annotations

from collections import deque
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from itertools import islice
import json
import math
import sys
import threading
import time


def encoded(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


def sample_level(count):
    """Dense first eight events, then 1/16, then 1/512 after event 256."""
    if count <= 8:
        return "dense"
    if count <= 256:
        return "stride16" if count % 16 == 0 else None
    return "stride512" if count % 512 == 0 else None


def preview(value, depth=0):
    kind = type(value)
    if value is None or kind is bool:
        return value
    if kind is int:
        return value if value.bit_length() <= 256 else {"type": "int", "bits": value.bit_length(), "truncated": True}
    if kind is float:
        return value if math.isfinite(value) else {"type": "float", "nonfinite": True}
    if kind is str:
        if len(value) <= 160:
            return value
        return {"type": "str", "length": len(value), "head": value[:160], "truncated": True}
    if kind is bytes:
        return {"type": "bytes", "length": len(value), "head_hex": value[:32].hex(), "truncated": len(value) > 32}
    if kind in (list, tuple, dict):
        result = {"type": kind.__name__, "length": len(value), "truncated": depth >= 2 or len(value) > 4}
        if depth < 2:
            if kind is dict:
                result["items"] = [[preview(k, depth + 1), preview(v, depth + 1)] for k, v in islice(value.items(), 4)]
            else:
                result["items"] = [preview(v, depth + 1) for v in islice(value, 4)]
            children = [part for pair in result["items"] for part in pair] if kind is dict else result["items"]
            result["truncated"] = result["truncated"] or any(
                type(child) is dict and (child.get("truncated", False) or child.get("omitted", False))
                for child in children
            )
        if len(encoded(result)) > 768:
            return {"type": kind.__name__, "length": len(value), "truncated": True}
        return result
    # Unknown instances and container subclasses may implement hostile repr/iter.
    return {"type": type.__dict__["__name__"].__get__(kind)[:80], "omitted": True}


@dataclass(frozen=True)
class ProbeLimits:
    max_output_bytes: int = 64 * 1024
    max_context_tokens: int = 16 * 1024
    max_samples: int = 48
    max_log_samples: int = 24
    max_exception_frames: int = 8
    max_locals: int = 12

    def __post_init__(self):
        for name, value in vars(self).items():
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.effective_bytes < 4096:
            raise ValueError("at least 4096 bytes must be reserved for error evidence")

    @property
    def effective_bytes(self):
        # Conservative upper bound for byte-based tokenizers; not exact tokens.
        return min(self.max_output_bytes, self.max_context_tokens)


class CountingSink:
    """Baseline sink: equivalent UTF-8 byte accounting without probe sampling."""

    encoding = "utf-8"

    def __init__(self):
        self.bytes_seen = 0

    def write(self, text):
        self.bytes_seen += len(text.encode("utf-8", "replace"))
        return len(text)

    def flush(self):
        pass

    def isatty(self):
        return False


class SampledSink(CountingSink):
    def __init__(self, max_samples):
        super().__init__()
        self.events = 0
        self.samples = deque(maxlen=max_samples)
        self.oversized = []
        self.levels = {"dense": 0, "stride16": 0, "stride512": 0}
        self.sampled = 0
        self.repeated = 0
        self.last_short = None
        self.truncated_writes = 0

    def write(self, text):
        size = len(text.encode("utf-8", "replace"))
        self.bytes_seen += size
        if not text or text == "\n":
            return len(text)
        self.events += 1
        if len(text) <= 160:
            self.repeated += int(text == self.last_short)
            self.last_short = text
        else:
            self.last_short = None
        truncated = len(text) > 160
        self.truncated_writes += int(truncated)
        level = sample_level(self.events)
        if not level and not (truncated and len(self.oversized) < 2):
            return len(text)
        entry = {"event": self.events, "bytes": size, "head": text[:160], "truncated": truncated}
        if truncated and len(self.oversized) < 2:
            self.oversized.append(entry)
        if level:
            self.levels[level] += 1
            self.sampled += 1
            entry["level"] = level
            self.samples.append(entry)
        return len(text)

    def report(self):
        return {
            "bytes_seen": self.bytes_seen, "events": self.events,
            "sampled": self.sampled, "retained": len(self.samples),
            "decimated": self.events - self.sampled,
            "evicted": self.sampled - len(self.samples),
            "repeated": self.repeated, "truncated_writes": self.truncated_writes,
            "levels": self.levels, "oversized_examples": self.oversized,
            "samples": list(self.samples),
        }


class RuntimeProbe:
    """Call-event sampling plus terminal traceback snapshots for one thread.

    Only explicitly selected code objects are sampled. No per-line hook is used.
    Local samples are copied immediately; no live frame references survive run().
    Existing profilers are restored on all ordinary/exceptional return paths.
    """

    def __init__(self, functions, limits=None):
        self.codes = {function.__code__ for function in functions}
        self.limits = limits or ProbeLimits()
        self._used = False

    def _frame(self, frame, line=None):
        values = frame.f_locals
        code = frame.f_code
        arg_names = code.co_varnames[:code.co_argcount + code.co_kwonlyargcount]
        # Prioritize arguments before other locals; bound both count and values.
        names = list(dict.fromkeys([*arg_names, *values.keys()]))
        selected = names[:self.limits.max_locals]
        return {
            "function": code.co_name, "file": code.co_filename[-200:],
            "line": line if line is not None else frame.f_lineno,
            "arguments": [name for name in arg_names if name in selected],
            "locals": {name: preview(values[name]) for name in selected if name in values},
            "locals_omitted": max(0, len(names) - len(selected)),
        }

    def _profile(self, frame, event, arg):
        if event != "call" or frame.f_code not in self.codes:
            return
        code = frame.f_code
        state = self.counts[code]
        state["calls"] += 1
        level = sample_level(state["calls"])
        if level:
            state["sampled"] += 1
            state["levels"][level] += 1
            self.samples.append({
                "status": "running", "level": level, "call": state["calls"],
                "frame": self._frame(frame),
            })

    def _monitor(self, code, instruction_offset):
        if threading.get_ident() != self._owner_thread:
            return
        state = self.counts[code]
        state["calls"] += 1
        level = sample_level(state["calls"])
        if level:
            state["sampled"] += 1
            state["levels"][level] += 1
            self.samples.append({
                "status": "running", "level": level, "call": state["calls"],
                "frame": self._frame(sys._getframe(1)),
            })

    def _enable(self):
        monitoring = getattr(sys, "monitoring", None)
        if monitoring is not None:
            # Reserve an unused tool slot; never replace another active tool.
            for identifier in (3, 4):
                try:
                    monitoring.use_tool_id(identifier, "rd-platform-probe")
                except ValueError:
                    continue
                self._tool_id = identifier
                monitoring.register_callback(identifier, monitoring.events.PY_START, self._monitor)
                for code in self.codes:
                    monitoring.set_local_events(identifier, code, monitoring.events.PY_START)
                self.backend = "sys.monitoring/PY_START (selected code objects only)"
                return
        self._profile_enabled = True
        self.backend = "sys.setprofile/call (compatibility fallback)"
        sys.setprofile(self._profile)

    def _disable(self):
        if self._tool_id is not None:
            monitoring = sys.monitoring
            identifier = self._tool_id
            for code in self.codes:
                monitoring.set_local_events(identifier, code, 0)
            monitoring.register_callback(identifier, monitoring.events.PY_START, None)
            monitoring.free_tool_id(identifier)
            self._tool_id = None
        if self._profile_enabled:
            sys.setprofile(self._previous_profile)
            self._profile_enabled = False

    def _exception(self, error):
        frames = deque(maxlen=self.limits.max_exception_frames)
        seen = 0
        tb = BaseException.__traceback__.__get__(error)
        while tb is not None:
            if tb.tb_frame.f_code in self.codes:
                seen += 1
                frames.append(self._frame(tb.tb_frame, tb.tb_lineno))
            tb = tb.tb_next
        # Do not call str(error): custom exception methods may execute code.
        args = BaseException.args.__get__(error)
        message = args[0][:240] if args and type(args[0]) is str else "<see bounded arguments>"
        kind = type.__dict__["__name__"].__get__(type(error))[:80]
        return {
            "type": kind, "message": message, "arguments": preview(args),
            "message_truncated": bool(args and type(args[0]) is str and len(args[0]) > 240),
            "frames": list(frames), "frames_omitted": seen - len(frames),
            "traceback": "Traceback (most recent call last):\n" + "\n".join(
                f'  File "{frame["file"]}", line {frame["line"]}, in {frame["function"]}'
                for frame in frames
            ) + f"\n{kind}: {message}",
        }

    def run(self, function, *args, **kwargs):
        if self._used:
            raise RuntimeError("RuntimeProbe is single-use")
        self._used = True
        self.counts = {code: {"calls": 0, "sampled": 0, "levels": {"dense": 0, "stride16": 0, "stride512": 0}} for code in self.codes}
        self.samples = deque(maxlen=self.limits.max_samples)
        stdout = SampledSink(self.limits.max_log_samples)
        stderr = SampledSink(self.limits.max_log_samples)
        exception = None
        result = None
        self._previous_profile = sys.getprofile()
        self._profile_enabled = False
        self._tool_id = None
        self._owner_thread = threading.get_ident()
        started = time.perf_counter()
        try:
            with redirect_stdout(stdout), redirect_stderr(stderr):
                self._enable()
                try:
                    result = function(*args, **kwargs)
                except Exception as error:
                    self._disable()
                    exception = self._exception(error)
                finally:
                    self._disable()
        finally:
            self._disable()
        report = {
            "schema": 1, "status": "error" if exception else "ok",
            "backend": self.backend,
            "execution_seconds": time.perf_counter() - started,
            "result": preview(result), "exception": exception,
            "sampling": [{
                "function": code.co_name, "line": code.co_firstlineno,
                **counts, "decimated": counts["calls"] - counts["sampled"],
            } for code, counts in self.counts.items()],
            "samples": list(self.samples),
            "samples_evicted": sum(c["sampled"] for c in self.counts.values()) - len(self.samples),
            "logs": {"stdout": stdout.report(), "stderr": stderr.report()},
            "budget": {
                "max_bytes": self.limits.max_output_bytes,
                "max_context_tokens": self.limits.max_context_tokens,
                "token_count_method": "UTF-8-byte conservative upper bound; not model tokenization",
                "effective_bytes": self.limits.effective_bytes,
                "samples_trimmed": 0, "log_samples_trimmed": 0,
                "oversize_examples_trimmed": 0,
                "exception_locals_trimmed": 0, "truncated": False,
                "serialized_bytes": 0, "token_upper_bound": 0,
            },
        }
        return self._fit(report)

    def _fit(self, report):
        budget = report["budget"]
        budget["truncated"] = bool(
            report["samples_evicted"] or any(c["decimated"] for c in report["sampling"])
            or any(log["decimated"] or log["truncated_writes"] for log in report["logs"].values())
        )
        while True:
            # Reserve a newline for safe emission through print().
            size = len(encoded(report)) + 1
            if size <= self.limits.effective_bytes and budget["serialized_bytes"] != size:
                budget["serialized_bytes"] = size
                budget["token_upper_bound"] = size
                continue
            if size <= self.limits.effective_bytes:
                return report
            budget["truncated"] = True
            # Remove a batch using individually serialized entry sizes. Repeatedly
            # serializing the entire report per discarded sample is quadratic.
            target_size = self.limits.effective_bytes - 128
            if report["samples"]:
                while report["samples"] and size > target_size:
                    size -= len(encoded(report["samples"].pop(0))) + 1
                    budget["samples_trimmed"] += 1
                continue
            logs = report["logs"]
            target = max(logs.values(), key=lambda item: len(item["samples"]))
            if target["samples"]:
                while target["samples"] and size > target_size:
                    size -= len(encoded(target["samples"].pop(0))) + 1
                    target["retained"] -= 1
                    budget["log_samples_trimmed"] += 1
                continue
            target = next((log for log in logs.values() if log["oversized_examples"]), None)
            if target:
                target["oversized_examples"].pop()
                budget["oversize_examples_trimmed"] += 1
                continue
            # Keep innermost failure evidence longest. Any discarded local is tagged.
            frames = report["exception"]["frames"] if report["exception"] else []
            target = next((frame for frame in frames if len(frame["locals"]) > 1), None)
            if target:
                name = next(reversed(target["locals"]))
                del target["locals"][name]
                target["arguments"] = [arg for arg in target["arguments"] if arg != name]
                target["locals_omitted"] += 1
                budget["exception_locals_trimmed"] += 1
                continue
            raise RuntimeError("probe evidence cannot fit the configured budget without losing critical frames")
