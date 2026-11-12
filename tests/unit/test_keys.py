"""Keys of every type, oxide-only (plan 24, release 1; D-004, D-044, D-046).

The conformance suite (`tests/conformance/test_keys.py`) checks what diskcache does too. These check
what only oxide does: the exact-type rules and their exceptions, the nesting limit, the order keys of
different types iterate in, the key `on_decode_error` gets, and `NullCache` raising the same errors.
"""

from __future__ import annotations

import enum
import math
from collections import namedtuple
from pathlib import Path
from typing import Any

import hypothesis.strategies as st
import pytest
from diskcache_oxide import Cache, FanoutCache, Key, NullCache, register_type, unregister_type
from hypothesis import given

MAX_DEPTH = 32


class Color(enum.IntEnum):
    RED = 1


class Name(enum.StrEnum):
    A = 'a'


Pair = namedtuple('Pair', 'a b')


@pytest.fixture
def cache(tmp_path: Path) -> Any:
    with Cache(tmp_path / 'c') as c:
        yield c


def nested(depth: int) -> Any:
    key: Any = None
    for _ in range(depth):
        key = (key,)
    return key


REFUSED_TYPES = [
    pytest.param(Color.RED, id='int-enum'),
    pytest.param(Name.A, id='str-enum'),
    pytest.param(Pair(1, 2), id='namedtuple'),
    pytest.param(bytearray(b'a'), id='bytearray'),
    pytest.param(memoryview(b'a'), id='memoryview'),
    pytest.param([1], id='list'),
    pytest.param({1: 2}, id='dict'),
    pytest.param(frozenset({1}), id='frozenset'),
    pytest.param(2**63, id='int-past-64-bits'),
    pytest.param(-(2**63) - 1, id='int-below-64-bits'),
    pytest.param((1, [2]), id='tuple-holding-a-list'),
    pytest.param(object(), id='object'),
]
REFUSED_VALUES = [
    pytest.param(math.nan, id='nan'),
    pytest.param((1, math.nan), id='tuple-holding-nan'),
    pytest.param(nested(MAX_DEPTH + 1), id='nested-too-deep'),
]


@pytest.mark.parametrize('key', REFUSED_TYPES)
@pytest.mark.parametrize('make', [lambda p: Cache(p), lambda p: FanoutCache(p, shards=2), lambda p: NullCache()])
def test_a_key_of_no_key_type_raises_type_error_everywhere(tmp_path: Path, make: Any, key: Any) -> None:
    cache = make(tmp_path / 'c')
    for call in (
        lambda: cache.set(key, 1),
        lambda: cache.get(key),
        lambda: cache.add(key, 1),
        lambda: cache.delete(key),
        lambda: cache.pop(key),
        lambda: cache.touch(key),
        lambda: cache.incr(key),
    ):
        with pytest.raises(TypeError):
            call()
    cache.close()


@pytest.mark.parametrize('key', REFUSED_VALUES)
@pytest.mark.parametrize('make', [lambda p: Cache(p), lambda p: FanoutCache(p, shards=2), lambda p: NullCache()])
def test_a_nan_or_a_tuple_nested_too_deep_raises_value_error_everywhere(tmp_path: Path, make: Any, key: Any) -> None:
    cache = make(tmp_path / 'c')
    with pytest.raises(ValueError):
        cache.set(key, 1)
    with pytest.raises(ValueError):
        cache.get(key)
    cache.close()


def test_the_nesting_limit_is_inclusive(cache: Any) -> None:
    key = nested(MAX_DEPTH)
    cache.set(key, 'deep')
    assert cache.get(key) == 'deep'
    assert list(cache) == [key]


@pytest.mark.parametrize('key', ['\ud800', ('a', '\udfff')], ids=['str', 'in-a-tuple'])
def test_a_lone_surrogate_raises_unicode_encode_error(cache: Any, key: Any) -> None:
    with pytest.raises(UnicodeEncodeError):
        cache.set(key, 1)


def test_the_message_names_the_key_types(cache: Any) -> None:
    with pytest.raises(
        TypeError, match=r'cache keys must be str, bytes, int, float, bool, None, or a tuple of those, not list'
    ):
        cache.set([1], 1)


def test_keys_iterate_str_first_then_by_type_then_by_value(cache: Any) -> None:
    # D-046: every str key, then None, bools, ints, floats, bytes, then tuples; each in value order.
    keys: list[Key] = [(1,), b'b', 2.5, -3, True, None, 'z', 'a', (), b'', -1.5, False, 10, ('a', None)]
    for key in keys:
        cache.set(key, 1)
    # Tuples compare element by element, by type first: (1,) is before ('a', None).
    want = ['a', 'z', None, False, True, -3, 10, -1.5, 2.5, b'', b'b', (), (1,), ('a', None)]
    assert list(cache.iterkeys()) == want
    assert list(cache.iterkeys(reverse=True)) == want[::-1]
    assert list(cache.iterkeys(prefix='')) == ['a', 'z'], 'a prefix walks str keys only'
    assert list(cache) == keys, 'insertion order keeps every key type in place'
    with pytest.raises(TypeError):
        list(cache.iterkeys(prefix=b'a'))


def test_a_fanout_cache_merges_every_key_type_in_the_same_order(tmp_path: Path) -> None:
    keys: list[Key] = [(1,), b'b', 2.5, -3, True, None, 'z', 'a', (), b'', -1.5, False, 10, ('a', None)]
    with Cache(tmp_path / 'one') as one, FanoutCache(tmp_path / 'many', shards=4) as many:
        for key in keys:
            one.set(key, 1)
            many.set(key, 1)
        assert list(many.iterkeys()) == list(one.iterkeys())
        assert list(many.iterkeys(reverse=True)) == list(one.iterkeys(reverse=True))
        assert sorted(map(repr, many)) == sorted(map(repr, keys))


class Point:
    def __init__(self, x: int) -> None:
        self.x = x


def test_on_decode_error_gets_the_key_back_with_its_type(tmp_path: Path) -> None:
    register_type(Point, name='tests.keys.Point', encode=lambda p: p.x, decode=Point)
    calls: list[Any] = []
    try:
        with Cache(tmp_path / 'c') as writer:
            writer.set(('point', 1, None), Point(3))
    finally:
        unregister_type(Point)
    with Cache(tmp_path / 'c', on_decode_error=lambda key, exc: calls.append(key)) as reader:
        assert reader.get(('point', 1, None), 'miss') == 'miss'
    assert calls == [('point', 1, None)]
    assert type(calls[0][1]) is int


SCALARS = (
    st.none() | st.booleans() | st.integers(-(2**63), 2**63 - 1) | st.floats(allow_nan=False) | st.binary(max_size=8)
)
KEYS = st.text(max_size=8) | st.recursive(
    SCALARS, lambda inner: st.tuples(inner, inner) | st.tuples(inner) | st.just(()), max_leaves=8
)
# Inside a tuple a str is an element of its own, which a lone str key never is.
TUPLE_KEYS = st.tuples(KEYS, st.text(max_size=4) | SCALARS)


def same(a: Any, b: Any) -> bool:
    if type(a) is not type(b):
        return False
    if isinstance(a, tuple):
        return len(a) == len(b) and all(same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, float):
        return a == b  # -0.0 comes back as 0.0
    return a == b


@given(key=KEYS | TUPLE_KEYS)
def test_any_key_round_trips_through_set_get_and_iteration(tmp_path_factory: pytest.TempPathFactory, key: Any) -> None:
    with Cache(tmp_path_factory.mktemp('k')) as cache:
        cache.set(key, 'v')
        assert cache.get(key) == 'v'
        [back] = list(cache)
        assert same(back, key) or (back == 0.0 and key == 0.0), (back, key)
        [back] = list(cache.iterkeys())
        assert same(back, key) or (back == 0.0 and key == 0.0), (back, key)


def test_a_damaged_stored_key_raises_corrupt_key_error_on_iteration(tmp_path: Path) -> None:
    import subprocess
    import sys

    from diskcache_oxide import CorruptKeyError, CorruptValueError

    assert issubclass(CorruptKeyError, CorruptValueError)
    for name, make in (('one', lambda p: Cache(p)), ('many', lambda p: FanoutCache(p, shards=2))):
        directory = tmp_path / name
        with make(directory) as cache:
            cache.set('s', 1)
            cache.set(7, 1)
        # Damage every typed key, from another process (two SQLites in one process break locks).
        script = (
            'import sqlite3, sys, pathlib\n'
            'for db in pathlib.Path(sys.argv[1]).rglob("diskcache-oxide.sqlite"):\n'
            '    c = sqlite3.connect(db); c.execute("UPDATE cache SET key = x\'11\' WHERE key_type = 2"); c.commit()\n'
        )
        subprocess.run([sys.executable, '-c', script, str(directory)], check=True)
        with make(directory) as cache:
            for walk in (
                lambda: list(cache),
                lambda: list(cache.iterkeys()),
                lambda: list(cache.iterkeys(reverse=True)),
            ):
                with pytest.raises(CorruptKeyError, match='a stored key is damaged'):
                    walk()
            assert cache.get('s') == 1, 'the rest of the cache still works'
