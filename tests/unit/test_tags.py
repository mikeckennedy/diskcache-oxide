"""Tags, oxide-only (plan 24, release 3): exact types, how a bad tag is refused, keyword-only `tag=`,
damaged tags, `NullCache`, `memoize(tag=)` and the recipes. diskcache's behavior is in the
conformance suite (`tests/conformance/test_tags.py`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache, CorruptKeyError, FanoutCache, Lock, NullCache, barrier, memoize_stampede, throttle

TAGS = ['users', b'raw', 7, 2.5, True, False, -0.5, ('user', 42), (None, (b'', 1.5)), 2**63 - 1]


@pytest.fixture(params=['cache', 'fanout'])
def any_cache(request: pytest.FixtureRequest, tmp_path: Path) -> Any:
    cache = Cache(tmp_path) if request.param == 'cache' else FanoutCache(tmp_path, shards=4)
    yield cache
    cache.close()


@pytest.mark.parametrize('tag', TAGS, ids=repr)
def test_every_key_type_is_a_tag_and_reads_back_with_its_exact_type(any_cache: Any, tag: Any) -> None:
    any_cache.set('k', 'v', tag=tag)
    value, got = any_cache.get('k', tag=True)
    assert (value, got, type(got)) == ('v', tag, type(tag))
    big = b'x' * 100_000  # A value file: the tag is on the row either way.
    assert any_cache.add('big', big, tag=tag) is True
    assert any_cache.get('big', expire_time=True, tag=True) == (big, None, tag)
    assert any_cache.pop('big', tag=True) == (big, tag)


def test_tags_of_different_types_are_different_tags(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        for key, tag in [('int', 1), ('float', 1.0), ('bool', True), ('str', '1'), ('bytes', b'1'), ('tuple', (1,))]:
            cache.set(key, 0, tag=tag)
        tags = [cache.get(key, tag=True)[1] for key in ['int', 'float', 'bool', 'str', 'bytes', 'tuple']]
        assert [type(t) for t in tags] == [int, float, bool, str, bytes, tuple]


@pytest.mark.parametrize(
    ('tag', 'error'),
    [([1], TypeError), ({'a': 1}, TypeError), (float('nan'), ValueError), (2**63, TypeError), (object(), TypeError)],
    ids=['list', 'dict', 'nan', 'int-too-big', 'object'],
)
def test_a_bad_tag_raises_before_anything_is_written(any_cache: Any, tag: Any, error: type[Exception]) -> None:
    for call in (any_cache.set, any_cache.add):
        with pytest.raises(error, match='invalid tag'):
            call('k', 'v', tag=tag)
    assert len(any_cache) == 0


def test_tag_is_keyword_only(any_cache: Any) -> None:
    with pytest.raises(TypeError):
        any_cache.set('k', 'v', None, 't')
    with pytest.raises(TypeError):
        any_cache.get('k', None, False, True)


def test_a_damaged_tag_raises_corrupt_key_error_only_when_asked_for(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        cache.set('k', 'v', tag=('t', 1))
    db = sqlite3.connect(tmp_path / 'diskcache-oxide.sqlite')
    with db:
        db.execute("UPDATE cache SET tag = X'FF00' WHERE key = 'k'")
    db.close()
    with Cache(tmp_path) as cache:
        assert cache.get('k') == 'v'
        with pytest.raises(CorruptKeyError):
            cache.get('k', tag=True)


def test_a_null_cache_checks_tags_and_returns_none() -> None:
    cache = NullCache()
    assert cache.set('k', 'v', tag='t') is True
    assert cache.add('k', 'v', tag=('t', 1)) is True
    with pytest.raises(TypeError, match='invalid tag'):
        cache.set('k', 'v', tag=[1])  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValueError, match='invalid tag'):
        cache.add('k', 'v', tag=float('nan'))
    assert cache.get('k', tag=True) == (None, None)
    assert cache.get('k', 'd', expire_time=True, tag=True) == ('d', None, None)
    assert cache.pop('k', 'd', tag=True) == ('d', None)


def test_memoize_stores_its_tag_and_checks_it_when_decorating(any_cache: Any) -> None:
    @any_cache.memoize(tag='episodes')
    def episode(n: int) -> int:
        return n * 2

    assert episode(3) == 6
    assert any_cache.get(episode.__cache_key__(3), tag=True) == (6, 'episodes')
    with pytest.raises(TypeError, match='invalid tag'):
        any_cache.memoize(tag=[1])


def test_memoize_takes_tag_fourth_as_diskcache_does(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:

        @cache.memoize('name', False, None, 'positional')
        def f() -> int:
            return 1

        f()
        assert cache.get(f.__cache_key__(), tag=True) == (1, 'positional')


def test_the_recipes_store_their_tag(tmp_path: Path) -> None:
    now = [1000.0]
    with Cache(tmp_path) as cache:
        lock = Lock(cache, 'lock', tag='coordination')
        with lock:
            assert cache.get('lock', tag=True) == (None, 'coordination')

        @throttle(cache, 2, 1, name='bucket', tag='coordination', time_func=lambda: now[0], sleep_func=lambda _: None)
        def work() -> None:
            pass

        work()
        assert cache.get('bucket', tag=True)[1] == 'coordination'

        @memoize_stampede(cache, expire=60, tag='coordination')
        def square(n: int) -> int:
            return n * n

        square(2)
        assert cache.get(square.__cache_key__(2), tag=True)[1] == 'coordination'


def test_barrier_passes_its_tag_to_the_lock_factory(tmp_path: Path) -> None:
    made = []

    def factory(cache: Any, key: Any, expire: Any = None, tag: Any = None) -> Lock:
        made.append(tag)
        return Lock(cache, key, expire=expire, tag=tag)

    with Cache(tmp_path) as cache:

        @barrier(cache, factory, name='b', tag='t')
        def work() -> int:
            return 2

        assert work() == 2
        assert made == ['t']
