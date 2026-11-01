"""Agent revision made after inspecting iteration-00's real pytest feedback."""

import json


def filter_records_json(records, min_score=0, limit=None):
    if type(min_score) is not int:
        raise ValueError("min_score must be an integer")
    if limit is not None and (type(limit) is not int or limit < 0):
        raise ValueError("limit must be a nonnegative integer or None")
    selected = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        identifier = record.get("id")
        # Missing scores remain invalid instead of being converted to zero.
        score = record.get("score")
        if not isinstance(identifier, str) or not identifier:
            continue
        # bool subclasses int, so isinstance alone violates the contract.
        if type(score) is not int or record.get("active", True) is not True:
            continue
        if score < min_score:
            continue
        if identifier not in selected or score > selected[identifier]["score"]:
            selected[identifier] = {"id": identifier, "score": score}
    result = sorted(selected.values(), key=lambda item: (-item["score"], item["id"]))
    # Zero is a valid explicit limit; only None means unlimited.
    if limit is not None:
        result = result[:limit]
    return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
