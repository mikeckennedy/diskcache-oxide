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
def test_auto_mode_shrinks_the_wal_back_after_a_burst_with_readers_in_other_processes(tmp_path: Path) -> None:
    directory = tmp_path / 'c'
    cache = Cache(directory, size_limit=2**34, wal_size_limit=LIMIT, wal_checkpoint='auto')
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


def test_auto_mode_never_forces_a_checkpoint_on_a_healthy_workload(tmp_path: Path) -> None:
    # Readers in this process only, and a small limit: the scaled autocheckpoint keeps the WAL far
    # under the trigger, so no forced checkpoint (and no write pause) ever happens.
    cache = Cache(tmp_path / 'c', wal_size_limit=LIMIT, wal_checkpoint='auto')
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


def test_auto_mode_busy_checkpoint_never_fails_the_write_and_recovers_after(tmp_path: Path) -> None:
    from tests.conformance.proc_workers import SqlitePeer

    directory = tmp_path / 'c'
    cache = Cache(directory, wal_size_limit=LIMIT, wal_checkpoint='auto')
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
    cache = Cache(tmp_path / 'c', wal_size_limit=LIMIT, wal_checkpoint='auto')
    cache.set('k', 'v')
    before = cache._wal_stats()['checks']
    with cache._hold_write_transaction():
        for i in range(200):
            cache.set(f'h{i}', 'x' * 1_000)
    assert cache._wal_stats()['checks'] == before
    cache.close()


@pytest.mark.parametrize('bad', ['always', 'AUTO', '', None, True])
def test_wal_checkpoint_is_validated(tmp_path: Path, bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        Cache(tmp_path / 'c', wal_checkpoint=bad)  # type: ignore[arg-type]


@pytest.mark.parametrize('bad', [True, 0, -1, 1024, 2**20 - 1, 1.5, '64MiB'])
def test_wal_size_limit_is_validated(tmp_path: Path, bad: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        Cache(tmp_path / 'c', wal_size_limit=bad)  # type: ignore[arg-type]


def test_null_cache_accepts_and_ignores_wal_size_limit() -> None:
    from diskcache_oxide import NullCache

    NullCache(wal_size_limit=2**20)


# ---------------------------------------------------------------------------
# The default, wal_checkpoint='manual': writes never checkpoint; checkpoint() and close() do
# ---------------------------------------------------------------------------


def _burst(cache: Cache, rows: int = 20_000) -> None:
    body = 'x' * 2_000
    for i in range(rows):
        cache.set(f'k{i}', body)
    cache.clear()


@pytest.mark.timeout(120)
def test_by_default_writes_never_checkpoint_and_checkpoint_truncates_under_reader_processes(tmp_path: Path) -> None:
    directory = tmp_path / 'c'
    cache = Cache(directory, size_limit=2**34, wal_size_limit=LIMIT, timeout=10)
    cache.set('k0', 'v')
    ctx = multiprocessing.get_context('spawn')
    events = [ctx.Event() for _ in range(4)]
    readers = [ctx.Process(target=_reader, args=(str(directory), time.time() + 60, e)) for e in events]
    for p in readers:
        p.start()
    try:
        assert all(e.wait(30) for e in events), 'readers never started'
        _burst(cache)
        assert cache._wal_stats()['attempts'] == 0, 'a write ran a forced checkpoint in manual mode'
        # With readers in other processes, the burst may have left the WAL large; checkpoint()
        # resets it regardless.
        assert cache.checkpoint() is True
        assert _wal_bytes(directory) == 0, f'{_wal_bytes(directory)} bytes after checkpoint()'
    finally:
        for p in readers:
            p.terminate()
            p.join(10)
        cache.close()


def test_checkpoint_returns_false_instead_of_raising_while_a_peer_holds_a_snapshot(tmp_path: Path) -> None:
    from tests.conformance.proc_workers import SqlitePeer

    directory = tmp_path / 'c'
    cache = Cache(directory, timeout=0.3)
    for i in range(200):
        cache.set(f'k{i}', 'x' * 10_000)
    with SqlitePeer(directory / 'diskcache-oxide.sqlite') as peer:
        peer.begin('READ')
        cache.set('more', 'x' * 10_000)  # Frames newer than the peer's snapshot.
        started = time.monotonic()
        assert cache.checkpoint() is False
        assert time.monotonic() - started < 2, 'checkpoint() waited far past the cache timeout'
        stats = cache._wal_stats()
        # One call is one attempt in the counters, however many busy rounds it retried.
        assert (stats['attempts'], stats['busy'], stats['errors']) == (1, 1, 0), stats
        peer.rollback()
    assert cache.checkpoint() is True
    stats = cache._wal_stats()
    assert (stats['attempts'], stats['successes']) == (2, 1), stats
    cache.close()


def _swollen(directory: Path) -> Cache:
    """A cache whose WAL is past its trim size, left by a burst under a peer's open snapshot."""
    from tests.conformance.proc_workers import SqlitePeer

    cache = Cache(directory, wal_size_limit=LIMIT)
    cache.set('k', 'v')
    with SqlitePeer(directory / 'diskcache-oxide.sqlite') as peer:
        peer.begin('READ')
        for i in range(1_000):
            cache.set(f'b{i % 200}', 'x' * 20_000)
        peer.rollback()
    assert _wal_bytes(directory) > LIMIT // 2
    return cache


# In both close tests a peer keeps a connection open (in no transaction) while the cache closes.
# Otherwise this would be the last connection to the database, and SQLite itself checkpoints and
# deletes the WAL when the last connection closes, whatever `checkpoint` says.


def test_close_checkpoints_a_large_wal(tmp_path: Path) -> None:
    from tests.conformance.proc_workers import SqlitePeer

    cache = _swollen(tmp_path / 'c')
    with SqlitePeer(tmp_path / 'c' / 'diskcache-oxide.sqlite') as peer:
        peer.data_version()  # A query attaches the peer to the WAL; an idle connection doesn't count.
        cache.close()
        assert _wal_bytes(tmp_path / 'c') == 0


def test_close_without_checkpoint_leaves_the_wal_alone(tmp_path: Path) -> None:
    from tests.conformance.proc_workers import SqlitePeer

    cache = _swollen(tmp_path / 'c')
    before = _wal_bytes(tmp_path / 'c')
    with SqlitePeer(tmp_path / 'c' / 'diskcache-oxide.sqlite') as peer:
        peer.data_version()
        cache.close(checkpoint=False)
        assert _wal_bytes(tmp_path / 'c') == before


@pytest.mark.parametrize('how', ['close(checkpoint=False)', 'garbage collected'])
def test_the_last_close_on_a_large_wal_lets_sqlite_checkpoint_it(tmp_path: Path, how: str) -> None:
    # With no other connection on the file, closing the last one makes SQLite checkpoint and delete
    # the WAL itself. That copy runs with the interpreter released (the handle is released
    # detached when the WAL is large), and the data is all there afterwards.
    import gc

    cache = _swollen(tmp_path / 'c')
    if how == 'garbage collected':
        del cache  # CPython frees it here; collect() for other runtimes.
        gc.collect()
    else:
        cache.close(checkpoint=False)
    assert _wal_bytes(tmp_path / 'c') == 0
    reopened = Cache(tmp_path / 'c')
    assert reopened.get('k') == 'v'
    assert reopened.get('b199') == 'x' * 20_000
    reopened.close()


def test_closing_twice_is_harmless_and_the_cache_reopens_after(tmp_path: Path) -> None:
    from tests.conformance.proc_workers import SqlitePeer

    cache = _swollen(tmp_path / 'c')
    with SqlitePeer(tmp_path / 'c' / 'diskcache-oxide.sqlite') as peer:
        peer.data_version()
        cache.close()
        cache.close()
        cache.close(checkpoint=False)
        with cache:
            pass  # __exit__ closes an already-closed handle again.
        assert _wal_bytes(tmp_path / 'c') == 0
        assert cache.get('k') == 'v', 'a closed cache reopens on use'
        cache.close()
        cache.close()


def test_closing_one_handle_repeatedly_leaves_the_other_as_the_last(tmp_path: Path) -> None:
    from tests.conformance.proc_workers import SqlitePeer

    directory = tmp_path / 'c'
    first = _swollen(directory)
    second = Cache(directory, wal_size_limit=LIMIT)
    with SqlitePeer(directory / 'diskcache-oxide.sqlite') as peer:
        peer.data_version()
        for _ in range(3):
            first.close()
        # Still a handle open: closing the first must not have checkpointed (it wasn't the last)...
        assert _wal_bytes(directory) > LIMIT // 2
        assert second.get('k') == 'v'
        # ...and the second is now the last one, exactly once, so its close checkpoints.
        second.close()
        assert _wal_bytes(directory) == 0
    assert second.set('after', 1) is True and second.get('after') == 1
    second.close()


def test_null_cache_checkpoint_and_close_take_the_same_arguments() -> None:
    from diskcache_oxide import NullCache

    null = NullCache()
    assert null.checkpoint() is True
    null.close(checkpoint=False)


def test_checkpoint_waits_for_another_threads_transaction_and_then_succeeds(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=10)
    for i in range(100):
        cache.set(f'k{i}', 'x' * 5_000)
    entered, release = threading.Event(), threading.Event()

    def hold() -> None:
        with cache._hold_write_transaction():
            cache.set('held', 1)
            entered.set()
            release.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    assert entered.wait(10)
    threading.Timer(0.3, release.set).start()
    assert cache.checkpoint() is True, 'checkpoint() gave up instead of waiting for the held transaction'
    holder.join()
    assert _wal_bytes(tmp_path / 'c') == 0
    assert cache.get('held') == 1
    cache.close()


def test_checkpoint_inside_your_own_transaction_returns_false_at_once(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=10)
    cache.set('k', 'v')
    with cache._hold_write_transaction():
        started = time.monotonic()
        assert cache.checkpoint() is False
        assert time.monotonic() - started < 0.5
    cache.close()


def test_checkpoint_takes_its_own_timeout(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=60)
    cache.set('k', 'v')
    assert cache.checkpoint(timeout=0.5) is True
    with pytest.raises(ValueError):
        cache.checkpoint(timeout=-1)
    cache.close()
