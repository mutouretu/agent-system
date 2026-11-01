"""Fixed acceptance suite, frozen before the initial sandbox execution."""

import copy
import json

import pytest

from candidate import filter_records_json


@pytest.mark.parametrize("records,minimum,limit,expected", [
    ([], 0, None, []),
    ([{"id": "a", "score": 5}], 5, None, [{"id": "a", "score": 5}]),
    ([{"id": "a", "score": 4}], 5, None, []),
    ([{"id": "a", "score": -2}], -2, None, [{"id": "a", "score": -2}]),
    ([{"id": "a", "score": 0}], 0, None, [{"id": "a", "score": 0}]),
    ([{"id": "missing"}], 0, None, []),
    ([{"id": "a", "score": None}], 0, None, []),
    ([{"id": "true", "score": True}, {"id": "false", "score": False}], 0, None, []),
    ([{"id": "float", "score": 2.0}, {"id": "string", "score": "2"}], 0, None, []),
    ([{"id": "off", "score": 9, "active": False}, {"id": "one", "score": 9, "active": 1}], 0, None, []),
    ([{"id": "a", "score": 2}, {"id": "a", "score": 9}, {"id": "a", "score": 4}], 0, None, [{"id": "a", "score": 9}]),
    ([{"id": "b", "score": 2}, {"id": "a", "score": 2}], 0, None, [{"id": "a", "score": 2}, {"id": "b", "score": 2}]),
    ([{"id": "甲", "score": 3, "extra": "ignored"}], 0, None, [{"id": "甲", "score": 3}]),
    ([{"id": "a", "score": 1}], 0, 0, []),
    ([{"id": "a", "score": 1}, {"id": "b", "score": 2}], 0, 1, [{"id": "b", "score": 2}]),
    ([None, "x", 1, [], {"id": "", "score": 3}, {"id": 3, "score": 4}], 0, None, []),
], ids=[
    "empty", "inclusive-threshold", "below-threshold", "negative-score", "zero-score",
    "missing-score", "null-score", "bool-is-not-score", "noninteger-score", "strict-active",
    "deduplicate-max", "stable-id-order", "unicode-and-format", "limit-zero", "limit-one", "malformed-rows",
])
def test_filter_contract(records, minimum, limit, expected):
    original = copy.deepcopy(records)
    actual = filter_records_json(records, min_score=minimum, limit=limit)
    assert json.loads(actual) == expected
    assert actual == json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    assert records == original


@pytest.mark.parametrize("limit", [-1, True, 1.5, "1"])
def test_invalid_limit(limit):
    with pytest.raises(ValueError):
        filter_records_json([], limit=limit)


@pytest.mark.parametrize("minimum", [True, 0.5, "0", None])
def test_invalid_minimum(minimum):
    with pytest.raises(ValueError):
        filter_records_json([], min_score=minimum)
