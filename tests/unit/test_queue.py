"""The queue calls, oxide-only (plan 24, release 4; D-054 to D-057): heads this process can't read,
heads whose files are gone, racing pullers, transactions, the argument checks, `NullCache`, and the
protocols. diskcache's behavior is in the conformance suite (`tests/conformance/test_queue.py`).
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import (
    Cache,
    CacheLike,
    CorruptValueError,
    FanoutCache,
    ForeignValueError,
    NullCache,
    TransactionalCacheLike,
)

START = 500_000_000_000_000


def damage(directory: Path, key: Any, sql: str) -> None:
    """Runs `sql` (with `?` the key) on the row for `key`, as another build or a bad disk would."""
    db = sqlite3.connect(directory / 'diskcache-oxide.sqlite')
    with db:
        db.execute(sql, (key,))
    db.close()


def test_a_foreign_head_stays_and_the_policy_decides(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        key = cache.push('from a newer build', prefix='q')
        cache.push('next', prefix='q')
    damage(tmp_path, key, 'UPDATE cache SET mode = 99 WHERE key = ?')  # A mode only a newer build knows.
    with Cache(tmp_path) as cache:
        with pytest.raises(ForeignValueError):
            cache.pull(prefix='q')
        assert len(cache) == 2, 'kept for a build that can read it'
    with Cache(tmp_path, on_decode_error='miss') as cache:
        assert cache.pull(prefix='q') == (None, None), 'nothing this puller can take'
        assert cache.peek(prefix='q') == (None, None)
        assert len(cache) == 2


def test_an_unreadable_head_is_removed_and_the_pull_goes_on(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        bad = cache.push({'a': 1}, prefix='q')
        cache.push({'b': 2}, prefix='q')
        cache.push({'c': 3}, prefix='q')
    damage(tmp_path, bad, "UPDATE cache SET value = X'C1' WHERE key = ?")  # Not msgpack at all.
    with Cache(tmp_path, on_decode_error='miss') as cache:
        assert cache.pull(prefix='q') == ('q-500000000000001', {'b': 2}), 'went on to the next item'
        assert len(cache) == 1
    with Cache(tmp_path) as cache:
        bad = cache.push({'d': 4}, prefix='r')
    damage(tmp_path, bad, "UPDATE cache SET value = X'C1' WHERE key = ?")
    with Cache(tmp_path) as cache:
        with pytest.raises(CorruptValueError):
            cache.pull(prefix='r')
        assert cache.peek(prefix='r') == (None, None), 'removed before it was raised'


def test_a_head_whose_file_is_gone_is_passed_over(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        cache.push(b'x' * 200_000)
        cache.push('next')
        for path in tmp_path.rglob('*.val'):
            path.unlink()
        assert cache.peek() == (START + 1, 'next')
        assert cache.peekitem(last=False) == (START + 1, 'next')
        cache.push(b'y' * 200_000, side='front')
        for path in tmp_path.rglob('*.val'):
            path.unlink()
        assert cache.pull() == (START + 1, 'next')
        assert cache.pull() == (None, None)


def test_racing_pullers_deliver_every_item_exactly_once(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        items = 600
        for i in range(items):
            cache.push(i, prefix='jobs')
        got: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            while True:
                key, value = cache.pull(prefix='jobs')
                if key is None:
                    return
                with lock:
                    got.append(value)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert sorted(got) == list(range(items))


def test_push_and_pull_inside_a_transaction_and_a_rollback(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        cache.push('kept')
        with pytest.raises(RuntimeError, match='boom'), cache.transact():
            assert cache.pull() == (START, 'kept')
            cache.push('gone')
            raise RuntimeError('boom')
        assert cache.pull() == (START, 'kept'), 'the rollback put the pulled item back'
        assert cache.pull() == (None, None)


def test_push_past_the_end_raises_and_touches_nothing(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        cache[1] = 'the lowest number'
        cache[0] = 'a key of my own'
        with pytest.raises(OverflowError):
            cache.push('x', side='front')
        assert cache[0] == 'a key of my own'
        assert len(cache) == 2


def test_the_queue_calls_count_nothing_in_stats(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        cache.stats(enable=True)
        cache.push('a')
        cache.peek()
        cache.peekitem()
        cache.pull()
        cache.pull()
        assert cache.stats() == (0, 0)


def test_tags_and_expiry_ride_along(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        key = cache.push('v', prefix='q', expire=60, tag=('job', 1))
        assert cache.get(key, tag=True) == ('v', ('job', 1))
        (pulled, value), expire_time, tag = cache.pull(prefix='q', expire_time=True, tag=True)
        assert (pulled, value, tag) == (key, 'v', ('job', 1)) and expire_time is not None


@pytest.mark.parametrize('make', [lambda p: Cache(p), lambda p: NullCache(p)], ids=['Cache', 'NullCache'])
def test_bad_arguments_raise_on_both(tmp_path: Path, make: Any) -> None:
    cache = make(tmp_path)
    with pytest.raises(TypeError, match='prefix'):
        cache.push('v', prefix=1)
    with pytest.raises(ValueError, match="'front' or 'back'"):
        cache.push('v', side='middle')
    with pytest.raises(ValueError, match="'front' or 'back'"):
        cache.pull(side='top')
    with pytest.raises(TypeError, match='invalid tag'):
        cache.push('v', tag=[1])
    with pytest.raises(TypeError):
        cache.pull(None, None, 'front', True)  # expire_time is keyword-only
    cache.close()


def test_a_null_cache_queue_is_always_empty_and_never_forwards(tmp_path: Path) -> None:
    with Cache(tmp_path) as real:
        real.push('real item')
        null = NullCache(invalidate=real)
        assert null.push('v') == START
        assert null.push('v', prefix='q') == 'q-500000000000000'
        assert null.pull() == (None, None)
        assert null.pull(default='d', expire_time=True, tag=True) == ('d', None, None)
        assert null.peek(tag=True) == ((None, None), None)
        with pytest.raises(KeyError):
            null.peekitem()
        assert real.peek() == (START, 'real item'), 'a switched-off cache never consumes a real queue'


def test_the_queue_calls_are_on_transactional_caches_only(tmp_path: Path) -> None:
    with Cache(tmp_path / 'c') as cache, FanoutCache(tmp_path / 'f', shards=2) as fanout:
        assert isinstance(cache, TransactionalCacheLike)
        assert isinstance(NullCache(), TransactionalCacheLike)
        assert isinstance(fanout, CacheLike), 'FanoutCache still satisfies CacheLike'
        assert not hasattr(fanout, 'push')


def test_peekitem_on_an_empty_cache_raises_key_error(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache, pytest.raises(KeyError, match='dictionary is empty'):
        cache.peekitem()
