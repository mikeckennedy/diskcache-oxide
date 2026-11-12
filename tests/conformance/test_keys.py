"""Keys of other types than `str` (plan 24, release 1).

diskcache stores `str`, `int`, `float` and `bytes` keys natively and pickles everything else. oxide
takes `str`, `bytes`, `int` (64-bit), `float`, `bool`, `None` and tuples of those, never pickled,
compared by exact type (D-044). Each test here passes on diskcache 5.6.3 as written.
"""

from __future__ import annotations

from typing import Any

import pytest

pytestmark = pytest.mark.requires('key_types')

KEYS = [
    pytest.param(0, id='int-zero'),
    pytest.param(-1, id='int-negative'),
    pytest.param(2**63 - 1, id='int-max'),
    pytest.param(-(2**63), id='int-min'),
    pytest.param(1.5, id='float'),
    pytest.param(-2.25, id='float-negative'),
    pytest.param(float('inf'), id='float-inf'),
    pytest.param(b'', id='bytes-empty'),
    pytest.param(b'\x00\xff\x00', id='bytes-nul'),
    pytest.param(True, id='true'),
    pytest.param(False, id='false'),
    pytest.param(None, id='none'),
    pytest.param((), id='tuple-empty'),
    pytest.param((1, 'a'), id='tuple'),
    pytest.param(('f', 1, None, 'b', 2), id='tuple-memoize-shape'),
    pytest.param((1, (2, (b'x', None)), 'é'), id='tuple-nested'),
    pytest.param(('a\x00b',), id='tuple-str-nul'),
]


def same(a: Any, b: Any) -> bool:
    """Equal and of the same type, all the way down a tuple."""
    if type(a) is not type(b):
        return False
    if isinstance(a, tuple):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    return a == b


@pytest.mark.parametrize('key', KEYS)
def test_a_key_round_trips(cache: Any, key: Any) -> None:
    assert cache.set(key, 'v') is True
    assert cache.get(key) == 'v'
    assert key in cache
    [stored] = list(cache)
    assert same(stored, key), stored
    assert cache.delete(key) is True
    assert key not in cache


@pytest.mark.parametrize('key', KEYS)
def test_every_keyed_call_takes_the_key(cache: Any, key: Any) -> None:
    assert cache.add(key, 1) is True
    assert cache.add(key, 2) is False
    assert cache.incr(key, 5) == 6
    assert cache.decr(key) == 5
    assert cache.touch(key, expire=60) is True
    assert cache.get(key, expire_time=True)[1] is not None
    assert cache.pop(key) == 5
    assert cache.get(key) is None
    cache[key] = 'x'
    assert cache[key] == 'x'
    del cache[key]
    assert len(cache) == 0


def test_keys_of_different_types_are_different_keys(cache: Any) -> None:
    keys = ['1', 1, b'1', ('1',), (1,), True, None, ()]
    for i, key in enumerate(keys):
        cache.set(key, i)
    for i, key in enumerate(keys):
        assert cache.get(key) == i, key
    assert len(cache) == len(keys)


def test_iteration_gives_back_each_key_with_its_type(cache: Any) -> None:
    keys = ['s', 7, -7, 2.5, b'b', None, True, ('t', 1, None), ((1,), ())]
    for key in keys:
        cache.set(key, 1)
    for listed in (list(cache), list(cache.iterkeys())):
        assert len(listed) == len(keys)
        for key in keys:
            assert any(same(key, got) for got in listed), (key, listed)


def test_negative_zero_is_zero(cache: Any) -> None:
    cache.set(0.0, 'zero')
    assert cache.get(-0.0) == 'zero'
    cache.set(-0.0, 'again')
    assert len(cache) == 1


@pytest.mark.diverges(backend='oxide', ref='D-044', reason='1 and 1.0 are two keys', raises=AssertionError)
def test_an_int_and_an_equal_float_are_one_key(cache: Any) -> None:
    cache.set(1, 'int')
    cache.set(1.0, 'float')
    assert len(cache) == 1
    assert cache.get(1) == 'float'


def test_a_bool_is_not_the_int_it_equals(cache: Any) -> None:
    cache.set(1, 'int')
    cache.set(True, 'bool')
    cache.set(0, 'zero')
    cache.set(False, 'false')
    assert (cache.get(1), cache.get(True), cache.get(0), cache.get(False)) == ('int', 'bool', 'zero', 'false')


@pytest.mark.diverges(backend='oxide', ref='D-004', reason='only the native key types', raises=TypeError)
@pytest.mark.parametrize(
    'key',
    [
        pytest.param(2**64, id='int-past-64-bits'),
        pytest.param(frozenset({1}), id='frozenset'),
        pytest.param((1, frozenset({2})), id='tuple-holding-frozenset'),
    ],
)
def test_other_hashable_keys_round_trip(cache: Any, key: Any) -> None:
    assert cache.set(key, 'v') is True
    assert cache.get(key) == 'v'


@pytest.mark.diverges(backend='oxide', ref='D-046', reason='str keys first, then by type', raises=AssertionError)
def test_iterkeys_orders_numbers_then_text_then_bytes(cache: Any) -> None:
    for key in ['b', 10, b'x', 1.5, 'a', 2]:
        cache.set(key, 1)
    # SQLite's order over the stored keys: ints and floats together, then text, then BLOBs.
    assert list(cache.iterkeys()) == [1.5, 2, 10, 'a', 'b', b'x']
