"""The queue calls, oxide-only (plan 24, release 4; D-054 to D-057): heads this process can't read,
heads whose files are gone, racing pullers, transactions, the argument checks, `NullCache`, and the
protocols. diskcache's behavior is in the conformance suite (`tests/conformance/test_queue.py`).
"""

from __future__ import annotations

import multiprocessing
import sqlite3
import threading
import time
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


def test_a_peek_that_removes_an_unreadable_head_reads_on(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        cache.set('older', 1)
        cache.set('newest', {'a': 1})
    damage(tmp_path, 'newest', "UPDATE cache SET value = X'C1' WHERE key = ?")
    with Cache(tmp_path, on_decode_error='miss') as cache:
        assert cache.peekitem() == ('older', 1), 'not KeyError: the cache isn’t empty'


def test_racing_pullers_never_remove_a_foreign_head(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        key = cache.push('from a newer build', prefix='q')  # A str key, so the damage can find it.
        cache.push('behind it', prefix='q')
    damage(tmp_path, key, 'UPDATE cache SET mode = 99 WHERE key = ?')
    with Cache(tmp_path, on_decode_error='miss') as cache:
        results: list[Any] = []

        def worker() -> None:
            for _ in range(50):
                results.append(cache.pull(prefix='q'))

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        assert set(results) == {(None, None)}
        assert len(cache) == 2, 'the foreign head and the item behind it are both still queued'


@pytest.mark.parametrize('make', [lambda p: Cache(p), lambda p: NullCache(p)], ids=['Cache', 'NullCache'])
def test_a_side_that_is_not_a_str_is_a_type_error_naming_it(tmp_path: Path, make: Any) -> None:
    cache = make(tmp_path)
    with pytest.raises(TypeError, match='side'):
        cache.push('v', side=1)
    cache.close()


def _produce(directory: str, worker: int, items: int) -> None:
    with Cache(directory) as cache:
        for i in range(items):
            side = 'back' if i % 2 else 'front'
            cache.push((worker, i, b'x' * 100_000), prefix='jobs', side=side)


def _consume(directory: str, results: Any, stop_after_idle: int) -> None:
    got = []
    idle = 0
    with Cache(directory) as cache:
        while idle < stop_after_idle:
            key, item = cache.pull(prefix='jobs', side='back' if len(got) % 2 else 'front')
            if key is None:
                idle += 1
                time.sleep(0.01)
                continue
            idle = 0
            got.append((item[0], item[1]))
    results.put(got)


@pytest.mark.slow
def test_pullers_in_many_processes_deliver_every_spilled_item_exactly_once(tmp_path: Path) -> None:
    spawn = multiprocessing.get_context('spawn')
    results = spawn.Queue()
    producers = [spawn.Process(target=_produce, args=(str(tmp_path), w, 150)) for w in range(4)]
    consumers = [spawn.Process(target=_consume, args=(str(tmp_path), results, 300)) for _ in range(8)]
    for p in producers + consumers:
        p.start()
    for p in producers:
        p.join(120)
    delivered = [item for _ in consumers for item in results.get(timeout=120)]
    for p in consumers:
        p.join(60)
    assert sorted(delivered) == sorted((w, i) for w in range(4) for i in range(150))
    with Cache(tmp_path) as cache:
        assert len(cache) == 0
    assert not list(tmp_path.rglob('*.val')), 'every value file went with its item'
