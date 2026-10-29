"""The WAL stays bounded (wal_size_limit), even with readers in other processes.

Found by the soak (Phase 2.5): with readers in other processes, a burst of writes (a fill, or a
`clear()`) can't wrap the WAL back to its start, so the file grows to fit the burst, and SQLite
never shrinks it afterwards. Reproduced with 4 reader processes: three bursts of 20k sets plus a
clear left a 538 MiB WAL against a few-MiB database, for good.

It needs readers truly running in parallel: on 3.14t the old behavior balloons (538 MiB to
1 GiB); on 3.14 the GIL leaves gaps that let normal resets happen (4.3 MiB), so there the burst test
is a bound check, and the regression it guards is caught by the 3.14t run in scripts/check.sh.
"""

from __future__ import annotations

import multiprocessing
import threading
import time
from pathlib import Path

import pytest

from diskcache_oxide import Cache

LIMIT = 4 * 2**20
WAL = 'diskcache-oxide.sqlite-wal'


def _reader(directory: str, stop_at: float, ready: object) -> None:
    from diskcache_oxide import Cache

    cache = Cache(directory)

    def loop() -> None:
        i = 0
        while time.time() < stop_at:
            cache.get(f'k{i % 1000}')
            i += 1

    threads = [threading.Thread(target=loop) for _ in range(4)]
    for t in threads:
        t.start()
    ready.set()  # type: ignore[attr-defined]
    for t in threads:
        t.join()


@pytest.mark.timeout(120)
def test_the_wal_shrinks_back_after_a_burst_with_readers_in_other_processes(tmp_path: Path) -> None:
    directory = tmp_path / 'c'
    cache = Cache(directory, size_limit=2**34, wal_size_limit=LIMIT)
    cache.set('k0', 'v')
    ctx = multiprocessing.get_context('spawn')
    stop_at = time.time() + 60
    events = [ctx.Event() for _ in range(4)]
    readers = [ctx.Process(target=_reader, args=(str(directory), stop_at, e)) for e in events]
    for p in readers:
        p.start()
    try:
        assert all(e.wait(30) for e in events), 'readers never started'
        body = 'x' * 2_000
        for i in range(20_000):  # A burst of writes, then a burst of deletes.
            cache.set(f'k{i}', body)
        cache.clear()
        # A trickle of writes afterwards gives forced checkpoints their chance.
        deadline = time.time() + 10
        size = (directory / WAL).stat().st_size
        while time.time() < deadline and size > 2 * LIMIT:
            cache.set('trickle', 'v')
            time.sleep(0.01)
            size = (directory / WAL).stat().st_size
        assert size <= 2 * LIMIT, f'the WAL is {size / 2**20:.0f} MiB after the burst (limit {LIMIT / 2**20:.0f} MiB)'
        # Under readers in other processes, a fast writer (3.14t) needs forced checkpoints, and they
        # must succeed. A slower one (3.14) can leave gaps for normal resets and need none.
        stats = cache._wal_stats()
        assert stats['errors'] == 0 and (stats['attempts'] == 0 or stats['successes'] >= 1), stats
    finally:
        for p in readers:
            p.terminate()
            p.join(10)
        cache.close()


def _wal_bytes(directory: Path) -> int:
    path = directory / WAL
    return path.stat().st_size if path.exists() else 0


def test_a_healthy_workload_never_forces_a_checkpoint(tmp_path: Path) -> None:
    # Readers in this process only, and a small limit: the scaled autocheckpoint keeps the WAL far
    # under the trigger, so no forced checkpoint (and no write pause) ever happens.
    cache = Cache(tmp_path / 'c', wal_size_limit=LIMIT)
    stop = threading.Event()

    def read() -> None:
        while not stop.is_set():
            cache.get('k')

    readers = [threading.Thread(target=read) for _ in range(4)]
    for t in readers:
        t.start()
    for i in range(5_000):
        cache.set(f'k{i % 100}', 'x' * 2_000)
    stop.set()
    for t in readers:
        t.join()
    stats = cache._wal_stats()
    assert stats['checks'] > 0 and stats['attempts'] == 0, stats
    assert _wal_bytes(tmp_path / 'c') <= LIMIT
    cache.close()


def test_a_busy_checkpoint_never_fails_the_write_and_recovers_after(tmp_path: Path) -> None:
    from tests.conformance.proc_workers import SqlitePeer

    directory = tmp_path / 'c'
    cache = Cache(directory, wal_size_limit=LIMIT)
    cache.set('k', 'v')
    body = 'x' * 20_000
    with SqlitePeer(directory / 'diskcache-oxide.sqlite') as peer:
        peer.begin('READ')  # Another process's open snapshot: the WAL can't reset under it.
        started = time.monotonic()
        for i in range(1_500):
            assert cache.set(f'b{i % 300}', body) is True
        elapsed = time.monotonic() - started
        held = cache._wal_stats()
        # Busy comes back as a row (busy = 1), never as an error.
        assert held['attempts'] >= 1 and held['busy'] >= 1 and held['successes'] == 0 and held['errors'] == 0, held
        # After a busy attempt the store backs off for a second, so attempts stay bounded by time.
        assert held['attempts'] <= elapsed + 2, (held, elapsed)
        assert _wal_bytes(directory) > LIMIT, 'the WAL should have grown past the limit under the open snapshot'
        peer.rollback()
    # Once the snapshot is gone, the WAL comes back under the limit within a few writes: through a
    # normal reset (the scaled autocheckpoint, then journal_size_limit) or a forced checkpoint.
    deadline = time.time() + 10
    while _wal_bytes(directory) > LIMIT and time.time() < deadline:
        cache.set('after', body)
        time.sleep(0.02)
    stats = cache._wal_stats()
    assert _wal_bytes(directory) <= LIMIT, f'{_wal_bytes(directory)} bytes, {stats}'
    assert stats['max_pause_us'] < 1_000_000, stats
    cache.close()


def test_writes_inside_a_held_transaction_never_checkpoint(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', wal_size_limit=LIMIT)
    cache.set('k', 'v')
    before = cache._wal_stats()['checks']
    with cache._hold_write_transaction():
        for i in range(200):
            cache.set(f'h{i}', 'x' * 1_000)
    assert cache._wal_stats()['checks'] == before
    cache.close()


@pytest.mark.parametrize('bad', [True, 0, -1, 1024, 2**20 - 1, 1.5, '64MiB'])
def test_wal_size_limit_is_validated(tmp_path: Path, bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        Cache(tmp_path / 'c', wal_size_limit=bad)  # type: ignore[arg-type]


def test_null_cache_accepts_and_ignores_wal_size_limit() -> None:
    from diskcache_oxide import NullCache

    NullCache(wal_size_limit=2**20)
