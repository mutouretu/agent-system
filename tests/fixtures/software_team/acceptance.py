"""Immutable demo acceptance; copied as tests/test_acceptance.py by the controller."""
import pytest
from lru_cache import LRUCache


@pytest.mark.parametrize("capacity", [True, False, 1.5, "2", None])
def test_capacity_type(capacity):
    with pytest.raises(TypeError):
        LRUCache(capacity)


@pytest.mark.parametrize("capacity", [0, -1, -100])
def test_capacity_value(capacity):
    with pytest.raises(ValueError):
        LRUCache(capacity)


def test_empty_and_default():
    cache = LRUCache(2)
    marker = object()
    assert len(cache) == 0 and cache.keys() == []
    assert cache.get("missing") is None
    assert cache.get("missing", marker) is marker


def test_eviction_and_hit_recency():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.keys() == ["a", "b"]
    assert cache.get("a") == 1
    assert cache.keys() == ["b", "a"]
    cache.put("c", 3)
    assert cache.get("b") is None
    assert cache.keys() == ["a", "c"] and len(cache) == 2


def test_update_does_not_evict_and_is_most_recent():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    cache.put("a", 10)
    assert cache.keys() == ["b", "a"] and len(cache) == 2
    cache.put("c", 3)
    assert cache.get("a") == 10 and cache.get("b") is None


def test_miss_does_not_touch_recency():
    cache = LRUCache(2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("absent", 7) == 7
    cache.put("c", 3)
    assert cache.keys() == ["b", "c"]


@pytest.mark.parametrize("value", [None, False, 0, "", [], {}])
def test_values_preserved(value):
    cache = LRUCache(1)
    cache.put("key", value)
    assert cache.get("key", object()) is value


def test_capacity_one_and_independent_instances():
    first, second = LRUCache(1), LRUCache(1)
    first.put("a", 1)
    first.put("b", 2)
    assert first.keys() == ["b"] and first.get("a") is None
    assert len(second) == 0


def test_keys_are_a_snapshot():
    cache = LRUCache(2)
    cache.put("a", 1)
    snapshot = cache.keys()
    snapshot.clear()
    assert cache.keys() == ["a"]


def test_hashable_keys_and_unhashable_error():
    cache = LRUCache(2)
    cache.put((1, 2), "tuple")
    cache.put(None, "none")
    assert cache.get((1, 2)) == "tuple" and cache.get(None) == "none"
    with pytest.raises(TypeError):
        cache.put([], 1)


def test_repeated_operations_keep_bound():
    cache = LRUCache(3)
    for index in range(200):
        cache.put(index, index * 2)
        assert len(cache) <= 3 and cache.get(index) == index * 2
    assert cache.keys() == [197, 198, 199]
