"""`stats()`, oxide-only (plan 24, release 2, decisions 7 and 19): per-process hit and miss counts.

What counts (`get`, `cache[k]`; a miss includes an expired entry and a value answered as a miss by
`on_decode_error`), what doesn't (`in`, `pop`, iteration, a durable outage), that every handle on a
directory in a process shares the counts and they survive closing the last one, exact counts from
many threads, a FanoutCache's sum, and a forked child starting at zero.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from pathlib import Path

from diskcache_oxide import Cache, FanoutCache, NullCache

from tests.unit.test_durable import peer_holding_the_write_lock


def test_only_get_and_getitem_count(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        cache.stats(enable=True)
        cache.set('k', 1)
        cache.set('gone', 1, expire=-1)
        cache.get('k')
        cache['k']
        cache.get('missing')
        cache.get('gone')
        'k' in cache  # noqa: B015
        cache.pop('k')
        list(cache)
        list(cache.iterkeys())
        assert cache.stats(reset=True) == (2, 2)


def test_handles_on_one_directory_share_the_counts_and_they_outlive_closing(tmp_path: Path) -> None:
    with Cache(tmp_path) as one:
        one.stats(enable=True)
        one.set('k', 1)
        one.get('k')
    with Cache(tmp_path) as two, Cache(tmp_path) as three:
        two.get('k')
        three.get('nope')
        assert three.stats(reset=True) == (2, 1), 'still on, and still counting, after the close'


def test_counts_are_exact_from_many_threads(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        cache.stats(enable=True, reset=True)
        cache.set('k', 1)

        def reader() -> None:
            for i in range(2_000):
                cache.get('k' if i % 2 else 'nope')

        threads = [threading.Thread(target=reader) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert cache.stats(reset=True) == (8_000, 8_000)


def test_a_read_during_another_processs_lock_counts(tmp_path: Path) -> None:
    with Cache(tmp_path, durable=True, timeout=0.05) as cache:
        cache.stats(enable=True, reset=True)
        cache.set('big', b'x' * 100_000)
        with peer_holding_the_write_lock(tmp_path):
            cache.get('k')  # A plain miss: reads don't need the lock.
        assert cache.stats() == (0, 1)


def test_a_fanout_cache_sums_its_shards(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=4) as cache:
        cache.stats(enable=True)
        for i in range(20):
            cache.set(f'k{i}', i)
        for i in range(30):
            cache.get(f'k{i}')
        assert cache.stats(reset=True) == (20, 10)
        assert cache.stats() == (0, 0)


def test_a_null_cache_reports_nothing() -> None:
    null = NullCache()
    null.get('k')
    assert null.stats() == (0, 0)


def test_a_forked_child_starts_from_zero(tmp_path: Path) -> None:
    # In a fresh process: never fork pytest itself (fork_scenarios.py's rule).
    proc = subprocess.run(
        [sys.executable, '-m', 'tests.unit.fork_scenarios', 'stats-after-fork', str(tmp_path / 'c')],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
