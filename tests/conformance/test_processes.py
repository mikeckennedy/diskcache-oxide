"""Multi-process behavior: racing processes, fork safety, worker overlap and spill-file concurrency.

Always uses an explicit multiprocessing context, because the default start method differs by
platform (spawn on macOS, forkserver on Linux since 3.14). Targets live in proc_workers.py so
spawned children can import them. Fork tests run in a fresh interpreter (see proc_workers.py).
"""

from __future__ import annotations

import contextlib
import multiprocessing
import random
import signal
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from pathlib import Path
from typing import Any

import pytest

from tests.conformance import proc_workers
from tests.conformance.backends import Backend
from tests.platforms import unix_only

REPO_ROOT = proc_workers.REPO_ROOT
JOIN_TIMEOUT = 30.0
POP_KEYS = 300
POP_TOKENS = 500
N_PROCS = 4

pytestmark = pytest.mark.timeout(60)

SPAWN = multiprocessing.get_context('spawn')


def _join_all(procs: list[Any]) -> None:
    for p in procs:
        p.join(JOIN_TIMEOUT)
    for p in procs:
        if p.is_alive():
            p.kill()
    assert [p.exitcode for p in procs] == [0] * len(procs)


@contextlib.contextmanager
def _reaped(done: Any, *groups: list[Any]) -> Generator[None]:
    """Always set `done` and kill any process still running on the way out, however the body exits.

    Readers loop until `done` is set, so a writer that dies (and makes `_join_all` raise) must not
    leave them spinning: pytest couldn't exit past non-daemon children.
    """
    try:
        yield
    finally:
        done.set()
        for proc in (p for group in groups for p in group):
            if proc.is_alive():
                proc.kill()
                proc.join(JOIN_TIMEOUT)


def _start(target: Callable[..., None], args_list: list[tuple[Any, ...]]) -> list[Any]:
    procs = [SPAWN.Process(target=target, args=args) for args in args_list]
    for p in procs:
        p.start()
    return procs


def _race_add(backend: Backend, cache_dir: Path, key: str, n: int = 8) -> list[int]:
    barrier = SPAWN.Barrier(n)
    results = SPAWN.Queue()
    procs = _start(
        proc_workers.add_race,
        [(backend.name, str(cache_dir), key, barrier, results)] * n,
    )
    outcomes = [results.get(timeout=JOIN_TIMEOUT) for _ in range(n)]
    _join_all(procs)
    return [pid for pid, won in outcomes if won]


def test_spawned_processes_racing_add_have_one_winner(backend: Backend, cache: Any, cache_dir: Path) -> None:
    winners = _race_add(backend, cache_dir, 'prize')
    assert len(winners) == 1
    assert cache.get('prize') == winners[0]


def test_spawned_processes_racing_add_on_an_expired_key_have_one_winner(
    backend: Backend, cache: Any, cache_dir: Path
) -> None:
    # The scheduled-job claim pattern: the previous claim has expired, and every worker tries to
    # take the next one with add(). Real time, because the children can't see a fake clock.
    assert cache.set('job', 'previous-claim', expire=0.2) is True
    time.sleep(0.3)
    assert cache.get('job') is None
    winners = _race_add(backend, cache_dir, 'job')
    assert len(winners) == 1, f'winners: {winners}'
    assert cache.get('job', expire_time=True) == (winners[0], None)


def test_spawned_processes_racing_pop_get_each_value_exactly_once(
    backend: Backend, cache: Any, cache_dir: Path
) -> None:
    # Every 20th value spills to a file.
    filler = 'x' * proc_workers.spill_min(backend)
    keys = [f'pop-{i}' for i in range(POP_KEYS)]
    values = {key: f'{key}|' + (filler if i % 20 == 0 else '') for i, key in enumerate(keys)}
    for key, value in values.items():
        cache.set(key, value)
    barrier = SPAWN.Barrier(N_PROCS)
    results = SPAWN.Queue()
    procs = _start(
        proc_workers.pop_many,
        [(backend.name, str(cache_dir), barrier, keys, results)] * N_PROCS,
    )
    popped = [value for _ in range(N_PROCS) for value in results.get(timeout=JOIN_TIMEOUT)]
    _join_all(procs)
    assert sorted(popped) == sorted(values.values())
    assert len(cache) == 0


def test_consumer_processes_racing_a_producer_never_get_a_value_twice(
    backend: Backend, cache: Any, cache_dir: Path
) -> None:
    # Several consumers read the same stored token, and only the delete can pick one of them.
    barrier = SPAWN.Barrier(N_PROCS + 1)
    produced = SPAWN.Event()
    results = SPAWN.Queue()
    producer = _start(
        proc_workers.pop_producer,
        [(backend.name, str(cache_dir), barrier, 'hot', POP_TOKENS, produced)],
    )
    consumers = _start(
        proc_workers.pop_consumer,
        [(backend.name, str(cache_dir), barrier, 'hot', produced, results)] * N_PROCS,
    )
    with _reaped(produced, producer, consumers):
        got = [value for _ in range(N_PROCS) for value in results.get(timeout=JOIN_TIMEOUT)]
        _join_all(producer + consumers)
    assert len(got) == len(set(got)), f'{len(got) - len(set(got))} values were delivered twice'
    assert got, 'nothing was popped'
    assert set(got) <= {f'token-{n}' for n in range(POP_TOKENS)}
    assert 'hot' not in cache


def test_spawned_processes_incr_total_is_exact(backend: Backend, cache: Any, cache_dir: Path) -> None:
    n, ops = 4, 250
    barrier = SPAWN.Barrier(n)
    procs = _start(proc_workers.incr_many, [(backend.name, str(cache_dir), barrier, ops)] * n)
    _join_all(procs)
    assert cache.get('counter') == n * ops


def _fork_check(backend: Backend, cache_dir: Path, mode: str) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            '-m',
            'tests.conformance.proc_workers',
            'fork',
            backend.name,
            str(cache_dir),
            mode,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=JOIN_TIMEOUT,
    )
    # Echoed so diagnostics (e.g. fork-in-write-txn's timings) show up in a failure or XPASS(strict) report.
    sys.stderr.write(proc.stderr)
    assert proc.returncode == 0, proc.stderr
    assert proc_workers.integrity(backend.db_path(cache_dir)) == 'ok'


@unix_only('fork')
@pytest.mark.parametrize('mode', ['os-fork', 'fork-context'])
def test_fork_safety(backend: Backend, make_cache: Callable[..., Any], cache_dir: Path, mode: str) -> None:
    # Parent uses the cache, forks two children that keep using the inherited Cache object, and all
    # three incr and write concurrently. The checks themselves run in the subprocess.
    _fork_check(backend, cache_dir, mode)
    reopened = make_cache(cache_dir)
    assert reopened.get('counter') == (proc_workers.FORK_CHILDREN + 1) * proc_workers.FORK_OPS


@unix_only('fork')
def test_fork_while_another_thread_reads_the_cache(
    backend: Backend, make_cache: Callable[..., Any], cache_dir: Path
) -> None:
    # A background thread hammers get on the shared Cache while the main thread forks. Each child
    # does a get and a set and must exit within proc_workers.CHILD_DEADLINE; a child that inherited
    # a held mutex deadlocks instead and is killed.
    _fork_check(backend, cache_dir, 'fork-while-reading')
    assert make_cache(cache_dir).get('from-child') == 'child'


@unix_only('fork')
@pytest.mark.diverges(
    backend='diskcache',
    ref='D-012',
    reason="a forked child's writes time out if another thread held a write transaction",
    raises=AssertionError,
)
def test_fork_while_another_thread_holds_a_write_transaction(
    backend: Backend, make_cache: Callable[..., Any], cache_dir: Path
) -> None:
    # Observed on diskcache, every time: the child's get works, but its set raises Timeout after the
    # cache's timeout, though the parent thread committed right after the fork. The parent is fine.
    _fork_check(backend, cache_dir, 'fork-in-write-txn')
    assert make_cache(cache_dir).get('from-child') == 'child'


@unix_only('fork')
@pytest.mark.covers('D-012')
@pytest.mark.backend_only('oxide')
def test_fork_while_another_thread_writes_the_cache(
    backend: Backend, make_cache: Callable[..., Any], cache_dir: Path
) -> None:
    # The same with a get/set hammer (D-012). Not run on diskcache: there the child's set times out
    # only when the fork happens to land inside one of the hammer's writes (about 20-50% of forks),
    # and a strict test can't pin a random outcome.
    _fork_check(backend, cache_dir, 'fork-while-writing')
    assert make_cache(cache_dir).get('from-child') == 'child'


@unix_only('fork')
def test_forked_child_closing_the_cache_does_not_break_the_parent(
    backend: Backend, make_cache: Callable[..., Any], cache_dir: Path
) -> None:
    # Children call close() on the inherited Cache and exit. The parent keeps writing and reading.
    _fork_check(backend, cache_dir, 'child-closes')
    reopened = make_cache(cache_dir)
    assert reopened.get('counter') == proc_workers.FORK_OPS
    assert len(reopened) == proc_workers.FORK_OPS + 2


def test_worker_overlap_sees_writes_from_a_process_that_exited(backend: Backend, cache_dir: Path) -> None:
    barrier = SPAWN.Barrier(2)
    writer_done = SPAWN.Event()
    results = SPAWN.Queue()
    holder = SPAWN.Process(
        target=proc_workers.overlap_holder,
        args=(backend.name, str(cache_dir), barrier, writer_done, results),
    )
    writer = SPAWN.Process(target=proc_workers.overlap_writer, args=(backend.name, str(cache_dir), barrier))
    holder.start()
    writer.start()
    writer.join(JOIN_TIMEOUT)
    assert writer.exitcode == 0
    writer_done.set()
    seen = results.get(timeout=JOIN_TIMEOUT)
    _join_all([holder])
    assert seen == ('B', b'b' * proc_workers.spill_min(backend), 3)


def _spill_run(backend: Backend, cache: Any, cache_dir: Path, writers: int, readers: int, ops: int) -> list[str]:
    keys = [f'blob-{i}' for i in range(8)]
    rng = random.Random(0)
    min_size = proc_workers.spill_min(backend)
    for key in keys:
        cache.set(key, proc_workers.make_blob(key, -1, 0, rng, min_size))
    barrier = SPAWN.Barrier(writers + readers)
    writers_done = SPAWN.Event()
    results = SPAWN.Queue()
    writer_procs = _start(
        proc_workers.spill_writer,
        [(backend.name, str(cache_dir), barrier, w, keys, ops) for w in range(writers)],
    )
    reader_procs = _start(
        proc_workers.spill_reader,
        [(backend.name, str(cache_dir), barrier, keys, writers_done, results)] * readers,
    )
    with _reaped(writers_done, writer_procs, reader_procs):
        _join_all(writer_procs)
        writers_done.set()
        reports = [results.get(timeout=JOIN_TIMEOUT) for _ in range(readers)]
        _join_all(reader_procs)

    torn = [t for _, _, report_torn in reports for t in report_torn]
    assert torn == [], 'torn reads'
    assert sum(hits for hits, _, _ in reports) > 0, 'readers never saw a value; the test proved nothing'

    live = [key for key in keys if cache.get(key) is not None]
    for key in live:
        assert proc_workers.blob_ok(key, cache.get(key), min_size)
    return live


@pytest.mark.diverges(
    backend='diskcache',
    ref='D-058',
    platform='win32',
    reason='a value file deleted under a reader is leaked on Windows',
    raises=AssertionError,
)
@pytest.mark.diverges(
    backend='community',
    ref='D-058',
    platform='win32',
    reason='a value file deleted under a reader is leaked on Windows',
    raises=AssertionError,
)
def test_spill_files_concurrent_overwrite_and_delete(backend: Backend, cache: Any, cache_dir: Path) -> None:
    live = _spill_run(backend, cache, cache_dir, writers=4, readers=3, ops=400)
    # Every value is at least twice the spill threshold, so each live row has exactly one value file
    # and nothing else should remain.
    assert len(backend.spill_files(cache_dir)) == len(live)


@pytest.mark.diverges(
    backend='diskcache',
    ref='D-058',
    platform='win32',
    reason='a value file deleted under a reader is leaked on Windows',
    raises=AssertionError,
)
@pytest.mark.diverges(
    backend='community',
    ref='D-058',
    platform='win32',
    reason='a value file deleted under a reader is leaked on Windows',
    raises=AssertionError,
)
@pytest.mark.slow
@pytest.mark.timeout(300)
def test_spill_files_concurrent_overwrite_and_delete_heavy(backend: Backend, cache: Any, cache_dir: Path) -> None:
    live = _spill_run(backend, cache, cache_dir, writers=8, readers=4, ops=4000)
    assert len(backend.spill_files(cache_dir)) == len(live)


# ---------------------------------------------------------------------------
# Phase 0.5
# ---------------------------------------------------------------------------


def _overwrite_race(
    backend: Backend, cache: Any, cache_dir: Path, readers: int, ops: int
) -> list[tuple[int, int, list[str]]]:
    key = 'hot'
    min_size = proc_workers.spill_min(backend)
    cache.set(key, proc_workers.make_blob(key, 0, 0, random.Random(0), min_size))
    barrier = SPAWN.Barrier(readers + 1)
    writer_done = SPAWN.Event()
    results = SPAWN.Queue()
    writer = _start(
        proc_workers.overwrite_writer,
        [(backend.name, str(cache_dir), barrier, key, ops)],
    )
    reader_procs = _start(
        proc_workers.overwrite_reader,
        [(backend.name, str(cache_dir), barrier, key, writer_done, results)] * readers,
    )
    with _reaped(writer_done, writer, reader_procs):
        _join_all(writer)
        writer_done.set()
        reports = [results.get(timeout=JOIN_TIMEOUT) for _ in range(readers)]
        _join_all(reader_procs)

    failures = [f for _, _, sample in reports for f in sample]
    assert sum(n for _, n, _ in reports) == 0, f'reads of an always-present key failed: {failures}'
    assert sum(hits for hits, _, _ in reports) > 0, 'readers never saw a value; the test proved nothing'
    # A reader that "cleaned up" a row it thought was orphaned would have deleted the live value.
    assert key in cache
    assert len(cache) == 1
    assert proc_workers.blob_ok(key, cache.get(key), min_size)
    assert len(backend.spill_files(cache_dir)) == 1, 'orphaned value files'
    return reports


@pytest.mark.covers('D-016')
@pytest.mark.backend_only('oxide')
def test_reading_a_spilled_value_while_it_is_overwritten_never_misses(
    backend: Backend, cache: Any, cache_dir: Path
) -> None:
    # Test A (D-016, upstream #258, #294). One writer overwrites one spilled key over and over; readers
    # get it with a sentinel default and must always see a whole value, old or new. Oxide-only: on
    # diskcache and the community fork an occasional read returns the default instead (this
    # variant catches it in about 15-45% of runs, the heavy one in every run), and a strict test
    # can't pin a random outcome.
    _overwrite_race(backend, cache, cache_dir, readers=4, ops=500)


@pytest.mark.slow
@pytest.mark.timeout(300)
@pytest.mark.covers('D-016')
@pytest.mark.backend_only('oxide')
def test_reading_a_spilled_value_while_it_is_overwritten_never_misses_heavy(
    backend: Backend, cache: Any, cache_dir: Path
) -> None:
    _overwrite_race(backend, cache, cache_dir, readers=6, ops=3000)


OPENERS = 8


def _open_new_directory_in_processes(backend: Backend, cache_dir: Path) -> None:
    barrier = SPAWN.Barrier(OPENERS)
    results = SPAWN.Queue()
    procs = _start(
        proc_workers.open_new_directory,
        [(backend.name, str(cache_dir), barrier, w, results) for w in range(OPENERS)],
    )
    outcomes = dict(results.get(timeout=JOIN_TIMEOUT) for _ in range(OPENERS))
    _join_all(procs)
    assert {w: err for w, err in outcomes.items() if err} == {}


def _open_new_directory_in_threads(backend: Backend, cache_dir: Path) -> None:
    barrier = threading.Barrier(OPENERS)
    errors: dict[int, BaseException] = {}

    def opener(worker: int) -> None:
        try:
            barrier.wait(JOIN_TIMEOUT)
            with backend.make_cache(cache_dir) as cache:
                cache.set(f'worker-{worker}', worker)
                assert cache.get(f'worker-{worker}') == worker
        except BaseException as ex:
            errors[worker] = ex

    threads = [threading.Thread(target=opener, args=(w,)) for w in range(OPENERS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_TIMEOUT)
    assert not any(t.is_alive() for t in threads)
    assert errors == {}


@pytest.mark.parametrize('how', ['processes', 'threads'])
def test_many_openers_racing_to_create_a_new_directory(
    backend: Backend, make_cache: Callable[..., Any], cache_dir: Path, how: str
) -> None:
    # Test B (upstream #266, #349). Eight openers hit a directory that doesn't exist yet at the same
    # moment, including the race to switch the new database to WAL. Nobody may fail, and every
    # write must survive.
    assert not cache_dir.exists()
    if how == 'processes':
        _open_new_directory_in_processes(backend, cache_dir)
    else:
        _open_new_directory_in_threads(backend, cache_dir)
    assert proc_workers.integrity(backend.db_path(cache_dir)) == 'ok'
    reopened = make_cache(cache_dir)
    assert {k: reopened.get(k) for k in reopened.iterkeys()} == {f'worker-{w}': w for w in range(OPENERS)}


@unix_only("mode bits; a Windows directory takes its parent's ACL")
@pytest.mark.backend_only('oxide', 'community')
def test_racing_creators_still_make_an_owner_only_directory(backend: Backend, cache_dir: Path) -> None:
    # Test B, D-015 side: the 0700 mode holds even when eight processes create the directory at once.
    _open_new_directory_in_processes(backend, cache_dir)
    assert stat.S_IMODE(cache_dir.stat().st_mode) == 0o700


@unix_only('fork')
def test_two_caches_on_one_directory_survive_an_idle_fork(
    backend: Backend, make_cache: Callable[..., Any], cache_dir: Path
) -> None:
    # Test C, variant 1 (upstream #325). Two Cache objects on one directory, both used, then a fork
    # while neither is busy. The child reads and writes through both while the parent keeps going.
    _fork_check(backend, cache_dir, 'two-caches-idle')
    assert make_cache(cache_dir).get('counter') == 2 * proc_workers.FORK_OPS


@unix_only('fork')
@pytest.mark.covers('D-012')
@pytest.mark.backend_only('oxide', 'community')
def test_fork_while_another_thread_writes_through_a_second_cache(
    backend: Backend, make_cache: Callable[..., Any], cache_dir: Path
) -> None:
    # Test C, variant 2 (D-012, upstream #325). A thread hammers writes through `b` while the main
    # thread forks using `a`, so the fork barrier must cover every Cache in the process. Not run on
    # diskcache: a child's write times out when its fork lands inside one of b's writes, which
    # happened in about half of the runs (two forks each).
    _fork_check(backend, cache_dir, 'two-caches-other-writing')
    reopened = make_cache(cache_dir)
    assert reopened.get('from-child') == 'child'
    assert reopened.get('from-child-b') == 'child'


@unix_only('fork')
def test_forked_child_can_open_and_use_a_new_cache(
    backend: Backend, make_cache: Callable[..., Any], cache_dir: Path
) -> None:
    # Test G. The parent used a cache, then forks; the child opens a brand-new Cache in another
    # directory and uses it. Mostly a smoke test for SQLite initialization after fork.
    _fork_check(backend, cache_dir, 'fork-then-new-cache')
    assert make_cache(cache_dir.parent / f'{cache_dir.name}-forked-child').get('small') == 'child'


@pytest.mark.backend_only('oxide')
def test_oxide_uses_its_bundled_sqlite(backend: Backend) -> None:
    # Test G, oxide side: guards against accidentally linking the system SQLite. The runtime version
    # comes from sqlite3_libversion() and the compile version from the SQLITE_VERSION macro (see
    # Backend.sqlite_info), so they differ if the extension loads a SQLite other than its own. The
    # version floor is a Phase 1 decision; update it when the bundled build is pinned.
    info = backend.sqlite_info()
    assert info is not None, 'oxide must report which SQLite it runs on'
    assert info['bundled'] is True
    assert info['runtime_version'] == info['compile_version'], f'linked against another SQLite: {info}'
    assert tuple(int(p) for p in info['runtime_version'].split('.')) >= (3, 45, 0)
    assert info['compile_options'], 'no compile options reported'


KILL_ROUNDS = 6


def _kill_writers_mid_spill(backend: Backend, make_cache: Callable[..., Any], cache_dir: Path, rounds: int) -> int:
    keys = [f'blob-{i}' for i in range(6)]
    min_size = proc_workers.spill_min(backend)
    rng = random.Random(0)
    for n in range(rounds):
        started = SPAWN.Event()
        (writer,) = _start(
            proc_workers.killable_spill_writer,
            [(backend.name, str(cache_dir), keys, n, started)],
        )
        assert started.wait(JOIN_TIMEOUT), 'writer never got going'
        time.sleep(rng.uniform(0, 0.05))
        writer.kill()
        writer.join(JOIN_TIMEOUT)
        # kill() is TerminateProcess on Windows, which multiprocessing reports as -SIGTERM.
        assert writer.exitcode == -(signal.SIGTERM if sys.platform == 'win32' else signal.SIGKILL)

    assert proc_workers.integrity(backend.db_path(cache_dir)) == 'ok'
    cache = make_cache(cache_dir)
    live = 0
    for key in keys:
        value = cache.get(key)  # must not raise
        assert value is None or proc_workers.blob_ok(key, value, min_size), f'{key} reads back corrupt'
        live += value is not None
    assert live > 0, 'no write ever committed; the test proved nothing'
    assert cache.set('after', b'a' * min_size) is True
    assert cache.get('after') == b'a' * min_size
    # Files the kill left behind (e.g. a value written but never committed) are allowed; Phase 6a's
    # check() cleans them up. Each live key and 'after' own one file.
    return len(backend.spill_files(cache_dir)) - (live + 1)


def test_writer_killed_mid_spill_leaves_a_consistent_cache(
    backend: Backend,
    make_cache: Callable[..., Any],
    cache_dir: Path,
    record_property: Any,
) -> None:
    # Test I. A writer process overwriting spilled values is SIGKILLed at a random moment, several
    # times (as deploy restarts do).
    record_property(
        'orphaned_files',
        _kill_writers_mid_spill(backend, make_cache, cache_dir, KILL_ROUNDS),
    )


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_writer_killed_mid_spill_leaves_a_consistent_cache_heavy(
    backend: Backend,
    make_cache: Callable[..., Any],
    cache_dir: Path,
    record_property: Any,
) -> None:
    record_property('orphaned_files', _kill_writers_mid_spill(backend, make_cache, cache_dir, 60))
