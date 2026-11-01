"""Deterministic CPU work, 10,000 logs, a large object and a four-frame error."""

import hashlib
import sys


def divide(iteration, record_id, numerator, denominator, payload):
    context_label = "normalizing-score"
    return numerator / denominator


def normalize(iteration, record_id, numerator, denominator, payload):
    operation = "normalize"
    return divide(iteration, record_id, numerator, denominator, payload)


def process_record(iteration, fail, payload):
    record_id = f"record-{iteration:05d}"
    # Real deterministic computation, not artificial sleep to hide probe overhead.
    digest = hashlib.pbkdf2_hmac("sha256", record_id.encode(), b"probe-integrity", 32)
    numerator = int.from_bytes(digest[:4], "big")
    denominator = 0 if fail and iteration == 9999 else 7
    return normalize(iteration, record_id, numerator, denominator, payload)


def run_batch(payload, fail=False, iterations=10_000):
    print(payload)
    print("starting batch", file=sys.stderr)
    total = 0.0
    for iteration in range(iterations):
        print("processed record")
        total += process_record(iteration, fail, payload)
    return total


FUNCTIONS = (run_batch, process_record, normalize, divide)
