"""Thread safety. Most meaningful on 3.14t (free-threaded), where the threads really run in parallel.

Each data-integrity scenario runs twice: every thread sharing one Cache object, and one Cache per
thread on the same directory. The fd scenarios (Test D) use one shared Cache. Counts were calibrated on 3.14t (16
threads x 1000 ops is about 0.5 s per scenario).
"""

from __future__ import annotations

import contextlib
import gc
import os
import subprocess
import sys
import threading
import time
import zlib
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import OXIDE_FD_BUDGET, Backend
from tests.conformance.proc_workers import REPO_ROOT

THREADS = 16
INCR_OPS = 1000
ADD_ROUNDS = 300
MIXED_OPS = 1000
MIXED_KEYS = 24
POP_KEYS = 300
POP_TOKENS = 500
JOIN_TIMEOUT = 30.0

LAYOUTS = pytest.mark.parametrize('layout', ['shared', 'per-thread'])


def _caches(layout: str, make_cache: Callable[..., Any], cache_dir: Path) -> list[Any]:
    if layout == 'shared':
        return [make_cache(cache_dir)] * THREADS
    return [make_cache(cache_dir) for _ in range(THREADS)]


def _run(target: Callable[[int, Any], None], caches: list[Any]) -> None:
    """Start one thread per cache together on a barrier; re-raise the first error any thread hit."""
    barrier = threading.Barrier(THREADS)
    errors: list[BaseException] = []

    def body(tid: int) -> None:
        try:
            barrier.wait(timeout=JOIN_TIMEOUT)
            target(tid, caches[tid])
        except BaseException as ex:
            errors.append(ex)
            barrier.abort()

    threads = [threading.Thread(target=body, args=(tid,), name=f'worker-{tid}') for tid in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT)
    assert not any(t.is_alive() for t in threads), 'a worker thread hung'
    if errors:
        raise errors[0]


@LAYOUTS
def test_incr_total_is_exact(layout: str, make_cache: Callable[..., Any], cache_dir: Path) -> None:
    caches = _caches(layout, make_cache, cache_dir)

    def work(tid: int, cache: Any) -> None:
        for _ in range(INCR_OPS):
            cache.incr('counter')

    _run(work, caches)
    assert caches[0].get('counter') == THREADS * INCR_OPS


@LAYOUTS
def test_racing_add_has_exactly_one_winner(layout: str, make_cache: Callable[..., Any], cache_dir: Path) -> None:
    caches = _caches(layout, make_cache, cache_dir)
    wins: list[list[int]] = [[] for _ in range(ADD_ROUNDS)]
    lock = threading.Lock()

    def work(tid: int, cache: Any) -> None:
        for r in range(ADD_ROUNDS):
            if cache.add(f'round-{r}', tid):
                with lock:
                    wins[r].append(tid)

    _run(work, caches)
    for r, winners in enumerate(wins):
        assert len(winners) == 1, f'round {r}: winners {winners}'
        # The stored value is the winner's, never overwritten by a loser.
        assert caches[0].get(f'round-{r}') == winners[0]


@LAYOUTS
def test_racing_pop_gets_each_value_exactly_once(
    layout: str, make_cache: Callable[..., Any], cache_dir: Path, backend: Any
) -> None:
    caches = _caches(layout, make_cache, cache_dir)
    filler = 'x' * (2 * backend.spill_threshold)  # Every 20th value is in a value file.
    values = {f'pop-{i}': f'pop-{i}|' + (filler if i % 20 == 0 else '') for i in range(POP_KEYS)}
    for key, value in values.items():
        caches[0].set(key, value)
    got: list[str] = []
    lock = threading.Lock()

    def work(tid: int, cache: Any) -> None:
        mine = [value for key in values if (value := cache.pop(key)) is not None]
        with lock:
            got.extend(mine)

    _run(work, caches)
    assert sorted(got) == sorted(values.values())
    assert len(caches[0]) == 0


# Defined only where threads run in parallel: a GIL build rarely overlaps this way, and test_processes.py
# has this race for those. Not a skip, since conftest.py fails the oxide run on any skip without a marker.
if not sys._is_gil_enabled():

    @LAYOUTS
    def test_consumers_racing_a_producer_never_get_a_value_twice(
        layout: str, make_cache: Callable[..., Any], cache_dir: Path
    ) -> None:
        # One producer and separate consumers, so several consumers read the same stored token and only
        # the delete can pick one of them. A thread that pops what it just set would prove nothing.
        caches = _caches(layout, make_cache, cache_dir)
        got: list[str] = []
        lock = threading.Lock()
        produced = threading.Event()

        def work(tid: int, cache: Any) -> None:
            if tid == 0:
                try:
                    for n in range(POP_TOKENS):
                        cache.set('hot', f'token-{n}')
                        time.sleep(0.0005)
                finally:
                    produced.set()
                return
            mine = []
            while not produced.is_set():
                if (value := cache.pop('hot')) is not None:
                    mine.append(value)
            if (value := cache.pop('hot')) is not None:
                mine.append(value)
            with lock:
                got.extend(mine)

        _run(work, caches)
        assert len(got) == len(set(got)), f'{len(got) - len(set(got))} values were delivered twice'
        assert got, 'nothing was popped'
        assert set(got) <= {f'token-{n}' for n in range(POP_TOKENS)}
        assert 'hot' not in caches[0]


def _mixed_value(key: str, tid: int, n: int, spill_size: int) -> str:
    # Every 10th value is twice the spill threshold, so it lands in a value file rather than the database row.
    body = f'{key}|{tid}|{n}|' + ('x' * spill_size if n % 10 == 0 else '')
    return f'{body}|{zlib.crc32(body.encode()):08x}'


def _check_mixed_value(key: str, value: Any) -> None:
    assert isinstance(value, str), f'{key}: {type(value)}'
    body, _, checksum = value.rpartition('|')
    assert body.split('|', 1)[0] == key, f'{key}: value belongs to another key'
    assert f'{zlib.crc32(body.encode()):08x}' == checksum, f'{key}: checksum mismatch'


@LAYOUTS
def test_mixed_get_set_delete_never_corrupts(
    layout: str, make_cache: Callable[..., Any], cache_dir: Path, backend: Backend
) -> None:
    caches = _caches(layout, make_cache, cache_dir)
    spill_size = 2 * backend.spill_threshold
    keys = [f'key-{i}' for i in range(MIXED_KEYS)]

    def work(tid: int, cache: Any) -> None:
        for n in range(MIXED_OPS):
            key = keys[(n * 7 + tid * 5) % MIXED_KEYS]
            op = (n + tid) % 4
            if op == 0:
                assert cache.set(key, _mixed_value(key, tid, n, spill_size)) is True
            elif op == 3:
                cache.delete(key)
            else:
                value = cache.get(key)
                if value is not None:
                    _check_mixed_value(key, value)

    _run(work, caches)
    survivors = 0
    for key in keys:
        value = caches[0].get(key)
        if value is not None:
            _check_mixed_value(key, value)
            survivors += 1
    assert len(caches[0]) == survivors


# ---------------------------------------------------------------------------
# Connections and fds are bounded, and closing leaves nothing behind (Test D)
# ---------------------------------------------------------------------------

FD_THREADS = 64  # kept well under macOS's default soft limit of 256 fds
FD_POLL_INTERVAL = 0.02
FD_STABLE_SAMPLES = 3
FD_DEADLINE = 5.0


def _fd_path(fd: int) -> str | None:
    try:
        if sys.platform == 'darwin':
            import fcntl

            return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b'\0').decode()
        return os.readlink(f'/proc/self/fd/{fd}')
    except OSError:
        return None


def _fds_inside(directory: Path) -> int:
    """How many of this process's open fds point at a path inside `directory`."""
    root = os.path.realpath(directory) + os.sep
    fd_dir = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    paths = (_fd_path(int(name)) for name in os.listdir(fd_dir))
    return sum(1 for path in paths if path is not None and os.path.realpath(path).startswith(root))


def _stable_fds_inside(directory: Path) -> int:
    """The fd count once it has stopped changing for a few samples (or at the deadline)."""
    deadline = time.monotonic() + FD_DEADLINE
    samples = [_fds_inside(directory)]
    while time.monotonic() < deadline:
        if len(samples) >= FD_STABLE_SAMPLES and len(set(samples[-FD_STABLE_SAMPLES:])) == 1:
            break
        time.sleep(FD_POLL_INTERVAL)
        samples.append(_fds_inside(directory))
    return samples[-1]


@contextlib.contextmanager
def _threads_held(cache: Any, threads: int) -> Generator[None]:
    """`threads` threads each do one get and one set on `cache`, then stay alive until the block exits."""
    ready = threading.Barrier(threads + 1)
    release = threading.Event()
    errors: list[BaseException] = []

    def body(tid: int) -> None:
        try:
            cache.get('main')
            assert cache.set(f'thread-{tid}', tid) is True
            ready.wait(JOIN_TIMEOUT)
            release.wait(JOIN_TIMEOUT)
        except BaseException as ex:
            errors.append(ex)
            ready.abort()

    workers = [threading.Thread(target=body, args=(tid,), name=f'fd-worker-{tid}') for tid in range(threads)]
    for t in workers:
        t.start()
    try:
        ready.wait(JOIN_TIMEOUT)
        yield
    finally:
        release.set()
        for t in workers:
            t.join(JOIN_TIMEOUT)
    assert not any(t.is_alive() for t in workers), 'a worker thread hung'
    if errors:
        raise errors[0]


def _peak_fds(make_cache: Callable[..., Any], cache_dir: Path) -> int:
    cache = make_cache(cache_dir)
    cache.set('main', 0)
    with _threads_held(cache, FD_THREADS):
        return _stable_fds_inside(cache_dir)


def test_peak_fds_within_the_backend_budget(make_cache: Callable[..., Any], cache_dir: Path, backend: Backend) -> None:
    # Pins the adapter's budget to what the backend actually does.
    assert _peak_fds(make_cache, cache_dir) <= backend.fd_budget(FD_THREADS)


@pytest.mark.diverges(
    backend='diskcache',
    ref='D-017',
    reason='diskcache keeps one connection per thread',
    raises=AssertionError,
)
def test_peak_fds_bounded_by_the_pool(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    peak = _peak_fds(make_cache, cache_dir)
    assert peak <= OXIDE_FD_BUDGET, f'{peak} fds open inside the cache directory with {FD_THREADS} threads'


@pytest.mark.diverges(
    backend='diskcache',
    ref='D-018',
    reason="close() leaves other threads' connections open",
    raises=AssertionError,
)
def test_close_releases_every_fd(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    cache = make_cache(cache_dir)
    cache.set('main', 0)
    # A gc pass would finalize leaked connections and hide whether close() released them.
    gc_was_enabled = gc.isenabled()
    gc.disable()
    try:
        with _threads_held(cache, FD_THREADS):
            pass
        cache.close()
        remaining = _stable_fds_inside(cache_dir)
    finally:
        if gc_was_enabled:
            gc.enable()
    assert remaining == 0, f'{remaining} fds still open inside the cache directory after close()'


@pytest.mark.backend_only('oxide')
@pytest.mark.covers('D-017')
def test_a_closed_handle_used_again_beside_a_live_one_adds_no_fd(
    make_cache: Callable[..., Any], cache_dir: Path
) -> None:
    """A closed handle used again rejoins its store. While another handle keeps the store live, that
    must open nothing: SQLite's unix VFS defers closing a descriptor on the database while any
    connection in the process holds a lock on it (every pooled connection does, in WAL mode), so a pin
    opened and closed for the rejoin stayed open as a bare fd on the database file, one over the
    budget, until the pool emptied (the soak's fd check, 2026-11-07)."""
    live = make_cache(cache_dir)
    live.set('k', 'v')
    assert live.get('k') == 'v'
    other = make_cache(cache_dir)
    assert other.get('k') == 'v'
    other.close()
    before = _stable_fds_inside(cache_dir)
    for _ in range(3):
        assert other.get('k') == 'v'  # Rejoins the live store.
        other.close()
    assert _stable_fds_inside(cache_dir) == before, 'a rejoin left an fd open on the database file'
    live.close()
    assert _stable_fds_inside(cache_dir) == 0


RACING_OPENERS = 16


@pytest.mark.backend_only('oxide')
@pytest.mark.covers('D-017')
def test_racing_first_opens_all_read_the_seed_and_close_leaves_no_fd(
    make_cache: Callable[..., Any], tmp_path: Path
) -> None:
    """Many threads opening one directory at once, each the process's first open of it: the openers
    that lose the race to register its store join the winner's and hand it their connection (plan 20),
    and closing every handle leaves no fd. Not within D-017's budget while they're open: openers that
    connected before any store registered each hold a connection, and those over the pool's idle cap
    are closed beside the live pool, where SQLite keeps them pending until the pool empties (D-017)."""
    for round_ in range(5):
        directory = tmp_path / f'race-{round_}'
        seed = make_cache(directory)
        seed.set('k', round_)
        seed.close()  # The database exists, and no store for it is live.
        barrier = threading.Barrier(RACING_OPENERS)
        handles: list[Any] = []
        seen: list[Any] = []
        lock = threading.Lock()

        def open_and_read(directory: Path = directory, barrier: threading.Barrier = barrier) -> None:
            barrier.wait()
            cache = make_cache(directory)
            value = cache.get('k')
            with lock:
                handles.append(cache)
                seen.append(value)

        threads = [threading.Thread(target=open_and_read) for _ in range(RACING_OPENERS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        assert seen == [round_] * RACING_OPENERS, seen
        assert len(handles) == RACING_OPENERS
        for cache in handles:
            cache.close()
        assert _stable_fds_inside(directory) == 0, 'closing every racing handle left an fd open'


@pytest.mark.diverges(
    backend='diskcache',
    ref='D-018',
    reason='unclosed per-thread connections warn at finalization',
    raises=AssertionError,
)
def test_threads_then_close_emit_no_resource_warning(backend: Backend, cache_dir: Path) -> None:
    # In a subprocess: sqlite3 reports these from dealloc as unraisables, and catch_warnings is
    # context-local on 3.14t, so an in-process check can't see them.
    proc = subprocess.run(
        [
            sys.executable,
            '-W',
            'error::ResourceWarning',
            '-m',
            'tests.conformance.proc_workers',
            'threads-close',
            backend.name,
            str(cache_dir),
            str(FD_THREADS),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=JOIN_TIMEOUT,
    )
    assert proc.returncode == 0, proc.stderr
    warnings = proc.stderr.count('ResourceWarning')
    assert warnings == 0, f'{warnings} ResourceWarning(s), first: {proc.stderr[:400]}'
