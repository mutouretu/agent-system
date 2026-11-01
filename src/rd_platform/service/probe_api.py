"""Dedicated single-worker process: RuntimeProbe's stream redirect stays local."""

import time

from fastapi import FastAPI, HTTPException
from rd_platform.runtime.probe import RuntimeProbe

app = FastAPI(title="R&D Runtime Probe", version="0.2.0")
_cached = None
_expires = 0.0


def _increment(value):
    return value + 1


def _self_check():
    value = 0
    for _ in range(16):
        value = _increment(value)
    return value


def snapshot():
    global _cached, _expires
    if _cached is None or time.monotonic() >= _expires:
        _cached = RuntimeProbe([_self_check, _increment]).run(_self_check)
        _expires = time.monotonic() + 5
    return _cached


@app.get("/health")
async def health():
    # No await in the probe section: one event-loop thread, one worker process.
    report = snapshot()
    if report["status"] != "ok" or report["result"] != 16:
        raise HTTPException(503, "runtime probe self-check failed")
    return {"status": "ok", "service": "probe", "runtime_probe": "ok", "version": "0.2.0"}


@app.get("/probe")
async def probe():
    return snapshot()
