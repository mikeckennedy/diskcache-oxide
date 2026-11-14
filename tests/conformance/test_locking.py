"""SQLite locking: write timeouts, lock-free reads, and chunked clear()/expire().

A second SQLite connection on `backend.db_path()` plays the other writer. It lives in a helper
subprocess (proc_workers.SqlitePeer), never in the test process, because two copies of SQLite in
one process (stdlib sqlite3 vs oxide's bundled rusqlite) would break each other's POSIX locks.

BEGIN IMMEDIATE takes the write lock (so cache writes must time out). BEGIN EXCLUSIVE is used for
the read tests: under WAL it still admits readers, while under rollback journaling it would lock
them out, so reads succeeding proves WAL mode. The read tests also check the peer's
PRAGMA data_version, which changes when any other connection commits, so a read that writes (or
tries to and swallows the failure) can't pass.
"""

import multiprocessing
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import BACKENDS, Backend
from tests.conformance.conftest import FakeClock
from tests.conformance.proc_workers import SqlitePeer
from tests.proctitle import titled

TIMEOUT = 0.05
UPPER_BOUND = 0.5
CHUNK_KEYS = 50_000
CHUNK_ATTEMPTS = 3


@contextmanager
def held_lock(db_path: Path, mode: str) -> Generator[SqlitePeer]:
    with SqlitePeer(db_path) as peer:
        peer.begin(mode)
        yield peer
        peer.rollback()


def big_value(backend: Backend) -> bytes:
    return b'x' * (2 * backend.spill_threshold)


@pytest.fixture
def prepared(make_cache: Callable[..., Any], cache_dir: Path, backend: Backend) -> Any:
    cache = make_cache(cache_dir, timeout=TIMEOUT)
    cache.set('k', 'v')
    cache.set('n', 1)
    cache.set('t', 't')
    cache.set('big', big_value(backend))
    assert backend.spill_files(cache_dir), 'expected the big value to spill to a file'
    return cache


def _add_expired_row(cache: Any, clock: FakeClock, cache_dir: Path, backend: Backend) -> None:
    # Created last: any later set would sweep it.
    cache.set('gone', 'x', expire=10)
    clock.advance(20)
    assert 'gone' not in cache
    assert backend.stored_rows(cache_dir) == 5, 'the expired row should still be stored'


WRITE_OPS: dict[str, Callable[[Any], Any]] = {
    'set': lambda c: c.set('k', 'other'),
    'set_new': lambda c: c.set('new', 'v'),
    'add': lambda c: c.add('new', 'v'),
    'incr': lambda c: c.incr('n'),
    'delete': lambda c: c.delete('k'),
    'clear': lambda c: c.clear(),
    'expire': lambda c: c.expire(),
    'touch': lambda c: c.touch('t', 10),
}


@pytest.mark.parametrize('op', WRITE_OPS.values(), ids=WRITE_OPS.keys())
def test_write_times_out_under_write_lock(
    prepared: Any,
    cache_dir: Path,
    backend: Backend,
    fake_clock: FakeClock,
    op: Callable[[Any], Any],
) -> None:
    # An expired row is present, so expire() (and the sweep in set) has real work that needs the lock.
    _add_expired_row(prepared, fake_clock, cache_dir, backend)
    with held_lock(backend.db_path(cache_dir), 'IMMEDIATE'):
        start = time.perf_counter()
        with pytest.raises(backend.timeout_error):
            op(prepared)
        # Observed about 60 ms for a 50 ms timeout. Only a generous upper bound is asserted.
        assert time.perf_counter() - start < UPPER_BOUND
    assert backend.stored_rows(cache_dir) == 5, 'the timed-out write changed the row count'


def test_writes_succeed_after_lock_released(prepared: Any, cache_dir: Path, backend: Backend) -> None:
    with held_lock(backend.db_path(cache_dir), 'IMMEDIATE'):
        with pytest.raises(backend.timeout_error):
            prepared.set('k', 'other')

    # The timed-out set changed nothing.
    assert prepared.get('k') == 'v'
    assert prepared.set('k', 'after') is True
    assert prepared.get('k') == 'after'
    assert prepared.incr('n') == 2


BIG = object()  # stands for big_value(backend) in READ_OPS


class Listed:
    """An expected count or listing that includes the expired 'gone' on diskcache and the fork, and
    leaves it out on oxide, which hides expired rows from len() and iteration (D-060)."""

    def __init__(self, stored: Any, live: Any) -> None:
        self.stored = stored
        self.live = live

    def on(self, backend: Backend) -> Any:
        return self.live if backend.name == 'oxide' else self.stored


READ_OPS: dict[str, tuple[Callable[[Any], Any], Any]] = {
    'get_hit': (lambda c: c.get('k'), 'v'),
    'get_expired': (lambda c: c.get('gone'), None),
    'get_missing': (lambda c: c.get('missing'), None),
    'get_spilled': (lambda c: c.get('big'), BIG),
    'contains': (lambda c: 'k' in c, True),
    'contains_expired': (lambda c: 'gone' in c, False),
    'len': (lambda c: len(c), Listed(5, 4)),
    'iterkeys': (lambda c: list(c.iterkeys()), Listed(['big', 'gone', 'k', 'n', 't'], ['big', 'k', 'n', 't'])),
    'iter': (lambda c: list(c), Listed(['k', 'n', 't', 'big', 'gone'], ['k', 'n', 't', 'big'])),
    'volume': (lambda c: c.volume() > 0, True),
}


@pytest.mark.parametrize('op, expected', READ_OPS.values(), ids=READ_OPS.keys())
def test_reads_never_write(
    prepared: Any,
    cache_dir: Path,
    backend: Backend,
    fake_clock: FakeClock,
    op: Callable[[Any], Any],
    expected: Any,
) -> None:
    if expected is BIG:
        expected = big_value(backend)
    elif isinstance(expected, Listed):
        expected = expected.on(backend)
    _add_expired_row(prepared, fake_clock, cache_dir, backend)

    with SqlitePeer(backend.db_path(cache_dir)) as peer:
        before = peer.data_version()
        # Under an exclusive lock: the read must not need the write lock.
        peer.begin('EXCLUSIVE')
        assert op(prepared) == expected
        peer.rollback()
        # And with no lock at all, where a write would succeed: nothing may be committed.
        assert op(prepared) == expected
        assert peer.data_version() == before, 'a read committed a write'


def test_volume_reads_under_exclusive_lock(prepared: Any, cache_dir: Path, backend: Backend) -> None:
    before = prepared.volume()
    with held_lock(backend.db_path(cache_dir), 'EXCLUSIVE'):
        assert prepared.volume() == before


def test_data_version_detects_a_cache_write(prepared: Any, cache_dir: Path, backend: Backend) -> None:
    # Guards the guard: the peer's data_version does move when the cache commits.
    with SqlitePeer(backend.db_path(cache_dir)) as peer:
        before = peer.data_version()
        prepared.set('k', 'changed')
        assert peer.data_version() != before


# ---------------------------------------------------------------------------
# Chunked clear() / expire()
#
# Observed on diskcache: both commit in chunks (a concurrent reader watches len() fall 100 rows at
# a time). A writer that waits in SQLite's busy handler (one set with timeout=1.0) usually does NOT
# get in between chunks; the handler sleeps and misses the short gaps, so the set completes only
# after the whole clear() finishes (about 90 ms for 50k keys). A writer that retries with a short
# timeout gets in every time. These tests therefore use the retrying writer, and assert that its
# sets commit while the maintenance call is still running.
#
# A faster backend may finish 50k keys before a chunk can be observed. That attempt proves
# nothing either way, so it is repeated with twice the keys (up to CHUNK_ATTEMPTS times).
#
# Also observed: once a writer is in, clear() keeps deleting rows inserted after it started, so
# an unbounded writer keeps clear() running indefinitely (D-011). The writer here stops after a
# few sets.
# ---------------------------------------------------------------------------


@titled('maintenance')
def _run_maintenance(backend_name: str, directory: str, op: str, done: Any, result: Any) -> None:
    cache = BACKENDS[backend_name]().make_cache(Path(directory))
    try:
        result.put(getattr(cache, op)())
    finally:
        done.set()
        cache.close()


def _populate(cache: Any, count: int, expire: Callable[[int], float] | None = None) -> None:
    def write() -> None:
        for i in range(count):
            cache.set(f'k{i}', i, expire=None if expire is None else expire(i))

    # transact() batches the sets into one transaction (about 10x faster) where available.
    if hasattr(cache, 'transact'):
        with cache.transact():
            write()
    else:
        write()


def _writes_during(cache: Any, backend: Backend, directory: Path, op: str, count: int) -> tuple[int, int] | None:
    """Run `op` in another process while this one retries sets. (landed, removed), or None if the
    call finished before it could be observed mid-run."""
    ctx = multiprocessing.get_context('spawn')
    done, result = ctx.Event(), ctx.Queue()
    proc = ctx.Process(target=_run_maintenance, args=(backend.name, str(directory), op, done, result))
    proc.start()
    try:
        # Wait until the first chunk has committed: from here on the call is mid-run.
        deadline = time.perf_counter() + 20
        while len(cache) == count and not done.is_set():
            assert time.perf_counter() < deadline, f'{op}() never started'

        landed = 0
        while landed < 3 and not done.is_set():
            try:
                cache.set(f'w{landed}', 'v')
            except backend.timeout_error:
                continue
            if not done.is_set():
                landed += 1

        removed = result.get(timeout=20)
    finally:
        proc.join(timeout=20)
        if proc.is_alive():
            proc.kill()
    assert proc.exitcode == 0
    return (landed, removed) if landed else None


def _chunked(
    cache: Any,
    backend: Backend,
    directory: Path,
    op: str,
    populate: Callable[[int], None],
) -> tuple[int, int, int]:
    count = CHUNK_KEYS
    for _ in range(CHUNK_ATTEMPTS):
        cache.clear()
        populate(count)
        assert len(cache) == count
        outcome = _writes_during(cache, backend, directory, op, count)
        if outcome is not None:
            return count, *outcome
        count *= 2
    pytest.fail(f'{op}() finished before any write could land, even with {count // 2} keys')


@pytest.mark.timeout(60)
def test_clear_releases_write_lock_between_chunks(
    make_cache: Callable[..., Any], cache_dir: Path, backend: Backend
) -> None:
    cache = make_cache(cache_dir, timeout=0.001)
    count, landed, removed = _chunked(cache, backend, cache_dir, 'clear', lambda n: _populate(cache, n))

    assert landed >= 1, 'no write committed while clear() was running'
    # clear() also counts rows the writer inserted after it started.
    assert removed >= count


@pytest.mark.timeout(60)
def test_expire_releases_write_lock_between_chunks(
    make_cache: Callable[..., Any],
    cache_dir: Path,
    backend: Backend,
    fake_clock: FakeClock,
) -> None:
    # Populate under a frozen clock ten seconds in the past, so no set sweeps the others. The
    # spawned process uses real time, where every entry is already expired. Expire times are kept
    # distinct: with identical expire times diskcache's expire() only removes 100 rows per call.
    fake_clock.advance(-10)
    cache = make_cache(cache_dir, timeout=0.001)
    count, landed, removed = _chunked(
        cache,
        backend,
        cache_dir,
        'expire',
        lambda n: _populate(cache, n, expire=lambda i: 0.001 + i * 1e-5),
    )

    assert landed >= 1, 'no write committed while expire() was running'
    # The writer's sets run under the frozen fake clock, where nothing has expired, so they sweep
    # nothing: the spawned expire() removed every populated row itself. The writer's own rows
    # never expire.
    assert removed == count
