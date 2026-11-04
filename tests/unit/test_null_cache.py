"""NullCache: every result follows from "every entry expires the instant it's written"."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from diskcache_oxide.null import NullCache
from diskcache_oxide.protocol import CacheLike


class Recorder:
    """A stand-in for a real cache that records the removal calls forwarded to it."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def delete(self, key: str) -> bool:
        self.calls.append(('delete', key))
        return True

    def clear(self) -> int:
        self.calls.append(('clear',))
        return 7

    def iterkeys(self, *, prefix: str | None = None, reverse: bool = False) -> Iterator[str]:
        self.calls.append(('iterkeys', prefix, reverse))
        return iter(['a', 'b'])


def test_reads_always_miss() -> None:
    cache = NullCache('/tmp/ignored')
    assert cache.get('k') is None
    assert cache.get('k', 'd') == 'd'
    assert cache.get('k', 'd', expire_time=True) == ('d', None)
    assert 'k' not in cache
    assert len(cache) == 0
    assert list(cache) == []
    assert list(cache.iterkeys()) == []
    with pytest.raises(KeyError):
        cache['k']


def test_writes_succeed_and_expire_instantly() -> None:
    cache = NullCache()
    assert cache.set('k', 'v', expire=10) is True
    assert cache.add('k', 'v') is True  # Absent again at once, so the add "succeeds" every time.
    assert cache.add('k', 'v') is True
    assert cache.get('k') is None
    cache['k'] = 'v'
    assert 'k' not in cache
    assert cache.touch('k', 10) is False
    assert cache.delete('k') is False
    with pytest.raises(KeyError):
        del cache['k']
    assert cache.pop('k') is None
    assert cache.pop('k', 'd') == 'd'


def test_incr_starts_fresh_every_time() -> None:
    cache = NullCache()
    assert cache.incr('c') == 1
    assert cache.incr('c') == 1
    assert cache.incr('c', 5, default=10) == 15
    assert cache.decr('c') == -1
    assert cache.incr('c', 0.5, default=1) == 1.5
    with pytest.raises(KeyError):
        cache.incr('c', default=None)


def test_maintenance_is_zero_and_it_is_falsy() -> None:
    cache = NullCache()
    assert (cache.expire(), cache.clear(), cache.cull(), cache.volume()) == (0, 0, 0, 0)
    assert not cache
    assert cache.enabled is False
    with cache as same:
        assert same is cache


def test_constructor_accepts_cache_arguments_and_reports_them() -> None:
    cache = NullCache(
        '/tmp/x',
        5,
        size_limit=2 << 20,
        cull_limit=3,
        serializer='json',
        pickle=True,
        clock=lambda: 0.0,
        max_connections=2,
    )
    assert (cache.directory, cache.timeout, cache.size_limit, cache.cull_limit) == (
        '/tmp/x',
        5.0,
        2 << 20,
        3,
    )
    defaults = NullCache()
    assert (defaults.directory, defaults.size_limit, defaults.cull_limit) == (
        None,
        2**30,
        10,
    )


def test_bad_argument_types_raise_like_a_real_cache() -> None:
    cache = NullCache()
    with pytest.raises(TypeError):
        cache.set(1, 'v')  # ty: ignore[invalid-argument-type]  # D-004
    with pytest.raises(TypeError):
        cache.set('k', 'v', expire=True)  # D-014
    with pytest.raises(TypeError):
        NullCache(timeout=True)  # D-014


def test_invalidate_forwards_removals_only() -> None:
    real = Recorder()
    cache = NullCache(invalidate=real)  # ty: ignore[invalid-argument-type]
    assert cache.delete('k') is True
    assert cache.pop('p', 'd') == 'd'
    assert cache.clear() == 7
    assert list(cache.iterkeys(prefix='a:')) == ['a', 'b']
    del cache['x']
    # Reads and writes stay null: nothing is forwarded for them.
    cache.set('k', 'v')
    assert cache.get('k') is None
    assert real.calls == [
        ('delete', 'k'),
        ('delete', 'p'),
        ('clear',),
        ('iterkeys', 'a:', False),
        ('delete', 'x'),
    ]


def test_close_never_closes_the_forwarding_target() -> None:
    class Closable(Recorder):
        closed = False

        def close(self) -> None:
            self.closed = True

    real = Closable()
    NullCache(invalidate=real).close()  # ty: ignore[invalid-argument-type]
    assert real.closed is False


def test_null_cache_satisfies_the_protocol() -> None:
    assert isinstance(NullCache(), CacheLike)
