"""NullCache: every result follows from "every entry expires the instant it's written"."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache
from diskcache_oxide.null import NullCache
from diskcache_oxide.protocol import CacheLike, TransactionalCacheLike


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


def test_null_cache_satisfies_the_transactional_protocol() -> None:
    assert isinstance(NullCache(), TransactionalCacheLike)


def test_transact_is_a_no_op_that_yields_the_null_cache() -> None:
    cache = NullCache()
    with cache.transact() as tx:
        assert tx is cache
        assert tx.set('k', 1) is True
        with cache.transact(retry=True) as inner, cache.transact(key='k') as keyed:
            assert inner is cache and keyed is cache
    assert cache.get('k') is None
    with pytest.raises(ValueError, match='boom'), cache.transact():
        raise ValueError('boom')


@pytest.mark.parametrize('retry', [1, 0, None, 'yes'])
def test_transact_refuses_a_retry_that_isnt_a_bool(retry: object) -> None:
    with pytest.raises(TypeError, match='retry'):
        NullCache().transact(retry=retry)  # ty: ignore[invalid-argument-type]


def test_transact_refuses_a_key_that_isnt_a_str() -> None:
    with pytest.raises(TypeError):
        NullCache().transact(key=1)  # ty: ignore[invalid-argument-type]


def test_transact_with_invalidate_opens_no_transaction_on_the_real_cache(tmp_path: Path) -> None:
    # Removals inside the block land at once, and the block's exception doesn't undo them. Another
    # thread's write to the real cache doesn't wait: no write lock is held.
    real = Cache(tmp_path / 'c', timeout=0.05)
    real.set('stale', 1)
    real.set('also-stale', 1)
    cache = NullCache(invalidate=real)
    with pytest.raises(ValueError), cache.transact():
        assert cache.delete('stale') is True
        assert real.get('stale') is None, 'the forwarded delete was not visible at once'
        written: list[object] = []
        thread = threading.Thread(target=lambda: written.append(real.set('other', 2)))
        thread.start()
        thread.join(10)
        assert written == [True], written
        cache.pop('also-stale')
        raise ValueError
    assert real.get('stale') is None and real.get('also-stale') is None, 'the exception undid a removal'
    assert real.get('other') == 2
    real.close()
