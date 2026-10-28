"""Multi-process behavior: racing processes, fork safety, worker overlap and spill-file concurrency.

Always uses an explicit multiprocessing context, because the default start method differs by
platform (spawn on macOS, forkserver on Linux since 3.14). Targets live in proc_workers.py so
spawned children can import them. Fork tests run in a fresh interpreter (see proc_workers.py).
"""

from __future__ import annotations

import multiprocessing
import random
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conformance import proc_workers
from tests.conformance.backends import Backend

REPO_ROOT = proc_workers.REPO_ROOT
JOIN_TIMEOUT = 30.0

pytestmark = pytest.mark.timeout(60)

SPAWN = multiprocessing.get_context('spawn')


def _join_all(procs: list[Any]) -> None:
    for p in procs:
        p.join(JOIN_TIMEOUT)
    for p in procs:
        if p.is_alive():
            p.kill()
    assert [p.exitcode for p in procs] == [0] * len(procs)


def _start(target: Callable[..., None], args_list: list[tuple[Any, ...]]) -> list[Any]:
    procs = [SPAWN.Process(target=target, args=args) for args in args_list]
    for p in procs:
        p.start()
    return procs


def _race_add(backend: Backend, cache_dir: Path, key: str, n: int = 8) -> list[int]:
    barrier = SPAWN.Barrier(n)
    results = SPAWN.Queue()
    procs = _start(proc_workers.add_race, [(backend.name, str(cache_dir), key, barrier, results)] * n)
    outcomes = [results.get(timeout=JOIN_TIMEOUT) for _ in range(n)]
    _join_all(procs)
    return [pid for pid, won in outcomes if won]


def test_spawned_processes_racing_add_have_one_winner(backend: Backend, cache: Any, cache_dir: Path) -> None:
    winners = _race_add(backend, cache_dir, 'prize')
    assert len(winners) == 1
    assert cache.get('prize') == winners[0]


def test_spawned_processes_racing_add_on_an_expired_key_have_one_winner(backend: Backend, cache: Any, cache_dir: Path) -> None:
    # The scheduled-job claim pattern: the previous claim has expired, and every worker tries to
    # take the next one with add(). Real time, because the children can't see a fake clock.
    assert cache.set('job', 'previous-claim', expire=0.2) is True
    time.sleep(0.3)
    assert cache.get('job') is None
    winners = _race_add(backend, cache_dir, 'job')
    assert len(winners) == 1, f'winners: {winners}'
    assert cache.get('job', expire_time=True) == (winners[0], None)


def test_spawned_processes_incr_total_is_exact(backend: Backend, cache: Any, cache_dir: Path) -> None:
    n, ops = 4, 250
    barrier = SPAWN.Barrier(n)
    procs = _start(proc_workers.incr_many, [(backend.name, str(cache_dir), barrier, ops)] * n)
    _join_all(procs)
    assert cache.get('counter') == n * ops


def _fork_check(backend: Backend, cache_dir: Path, mode: str) -> None:
    proc = subprocess.run(
        [sys.executable, '-m', 'tests.conformance.proc_workers', 'fork', backend.name, str(cache_dir), mode],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=JOIN_TIMEOUT,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc_workers.integrity(backend.db_path(cache_dir)) == 'ok'


@pytest.mark.parametrize('mode', ['os-fork', 'fork-context'])
def test_fork_safety(backend: Backend, make_cache: Callable[..., Any], cache_dir: Path, mode: str) -> None:
    # Parent uses the cache, forks two children that keep using the inherited Cache object, and all
    # three incr and write concurrently. The checks themselves run in the subprocess.
    _fork_check(backend, cache_dir, mode)
    reopened = make_cache(cache_dir)
    assert reopened.get('counter') == (proc_workers.FORK_CHILDREN + 1) * proc_workers.FORK_OPS


def test_fork_while_another_thread_reads_the_cache(backend: Backend, make_cache: Callable[..., Any], cache_dir: Path) -> None:
    # A background thread hammers get on the shared Cache while the main thread forks. Each child
    # does a get and a set and must exit within proc_workers.CHILD_DEADLINE; a child that inherited
    # a held mutex deadlocks instead and is killed.
    _fork_check(backend, cache_dir, 'fork-while-reading')
    assert make_cache(cache_dir).get('from-child') == 'child'


@pytest.mark.requires('transact')
@pytest.mark.diverges(
    backend='diskcache', ref='D-012', reason="a forked child's writes time out if another thread held a write transaction", raises=AssertionError
)
def test_fork_while_another_thread_holds_a_write_transaction(backend: Backend, make_cache: Callable[..., Any], cache_dir: Path) -> None:
    # Observed on diskcache, every time: the child's get works, but its set raises Timeout after the
    # cache's timeout, though the parent thread committed right after the fork. The parent is fine.
    _fork_check(backend, cache_dir, 'fork-in-write-txn')
    assert make_cache(cache_dir).get('from-child') == 'child'


@pytest.mark.backend_only('oxide')
def test_fork_while_another_thread_writes_the_cache(backend: Backend, make_cache: Callable[..., Any], cache_dir: Path) -> None:
    # The same with a get/set hammer (D-012). Not run on diskcache: there the child's set times out
    # only when the fork happens to land inside one of the hammer's writes (about 20-50% of forks),
    # and a strict test can't pin a random outcome.
    _fork_check(backend, cache_dir, 'fork-while-writing')
    assert make_cache(cache_dir).get('from-child') == 'child'


def test_forked_child_closing_the_cache_does_not_break_the_parent(backend: Backend, make_cache: Callable[..., Any], cache_dir: Path) -> None:
    # Children call close() on the inherited Cache and exit. The parent keeps writing and reading.
    _fork_check(backend, cache_dir, 'child-closes')
    reopened = make_cache(cache_dir)
    assert reopened.get('counter') == proc_workers.FORK_OPS
    assert len(reopened) == proc_workers.FORK_OPS + 2


def test_worker_overlap_sees_writes_from_a_process_that_exited(backend: Backend, cache_dir: Path) -> None:
    barrier = SPAWN.Barrier(2)
    writer_done = SPAWN.Event()
    results = SPAWN.Queue()
    holder = SPAWN.Process(target=proc_workers.overlap_holder, args=(backend.name, str(cache_dir), barrier, writer_done, results))
    writer = SPAWN.Process(target=proc_workers.overlap_writer, args=(backend.name, str(cache_dir), barrier))
    holder.start()
    writer.start()
    writer.join(JOIN_TIMEOUT)
    assert writer.exitcode == 0
    writer_done.set()
    seen = results.get(timeout=JOIN_TIMEOUT)
    _join_all([holder])
    assert seen == ('B', b'b' * proc_workers.SPILL_MIN, 3)


def _spill_run(backend: Backend, cache: Any, cache_dir: Path, writers: int, readers: int, ops: int) -> list[str]:
    keys = [f'blob-{i}' for i in range(8)]
    rng = random.Random(0)
    min_size = proc_workers.spill_min(backend)
    for key in keys:
        cache.set(key, proc_workers.make_blob(key, -1, 0, rng, min_size))
    barrier = SPAWN.Barrier(writers + readers)
    writers_done = SPAWN.Event()
    results = SPAWN.Queue()
    writer_procs = _start(proc_workers.spill_writer, [(backend.name, str(cache_dir), barrier, w, keys, ops) for w in range(writers)])
    reader_procs = _start(proc_workers.spill_reader, [(backend.name, str(cache_dir), barrier, keys, writers_done, results)] * readers)
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


def test_spill_files_concurrent_overwrite_and_delete(backend: Backend, cache: Any, cache_dir: Path) -> None:
    live = _spill_run(backend, cache, cache_dir, writers=4, readers=3, ops=400)
    # Every value is at least twice the spill threshold, so each live row has exactly one value file
    # and nothing else should remain.
    assert len(backend.spill_files(cache_dir)) == len(live)


@pytest.mark.slow
@pytest.mark.timeout(300)
def test_spill_files_concurrent_overwrite_and_delete_heavy(backend: Backend, cache: Any, cache_dir: Path) -> None:
    live = _spill_run(backend, cache, cache_dir, writers=8, readers=4, ops=4000)
    assert len(backend.spill_files(cache_dir)) == len(live)
