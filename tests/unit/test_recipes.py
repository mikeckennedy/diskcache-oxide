"""The recipes, oxide-only (plan 24, release 2; D-051): the refusals, waiting inside a transaction,
FanoutCache, `throttle`'s fixed bugs, `memoize_stampede` on a NullCache, and mutual exclusion across
processes. Single-process diskcache behavior is in the conformance suite.
"""

from __future__ import annotations

import multiprocessing
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import (
    Averager,
    BoundedSemaphore,
    Cache,
    FanoutCache,
    Lock,
    NullCache,
    RLock,
    barrier,
    memoize_stampede,
    throttle,
)

COORDINATION = [
    pytest.param(lambda c: Lock(c, 'k'), id='Lock'),
    pytest.param(lambda c: RLock(c, 'k'), id='RLock'),
    pytest.param(lambda c: BoundedSemaphore(c, 'k'), id='BoundedSemaphore'),
    pytest.param(lambda c: Averager(c, 'k'), id='Averager'),
    pytest.param(lambda c: throttle(c, 1, 1), id='throttle'),
]


@pytest.mark.parametrize('make', COORDINATION)
def test_coordination_refuses_a_null_cache(make: Any) -> None:
    with pytest.raises(TypeError, match='NullCache'):
        make(NullCache())


@pytest.mark.parametrize('make', COORDINATION)
def test_coordination_refuses_a_durable_handle(tmp_path: Path, make: Any) -> None:
    with Cache(tmp_path, durable=True) as cache, pytest.raises(TypeError, match='durable'):
        make(cache)


def test_tag_must_be_none_until_tags_arrive(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache, pytest.raises(TypeError, match='tag'):
        Lock(cache, 'k', tag='t')  # ty: ignore[invalid-argument-type]


def test_a_lock_that_would_wait_inside_its_caches_transaction_raises(tmp_path: Path) -> None:
    with Cache(tmp_path) as cache:
        lock = Lock(cache, 'k')
        lock.acquire()
        with cache.transact(), pytest.raises(RuntimeError, match='no other process could ever release'):
            Lock(cache, 'k').acquire()
        lock.release()
        with cache.transact():
            Lock(cache, 'free').acquire()  # Free: nothing to wait for, so fine.


@pytest.mark.parametrize('recipe', ['Lock', 'RLock', 'BoundedSemaphore', 'Averager'])
def test_recipes_work_on_a_fanout_cache(tmp_path: Path, recipe: str) -> None:
    with FanoutCache(tmp_path, shards=4) as cache:
        if recipe == 'Averager':
            average = Averager(cache, 'avg')
            average.add(1.0)
            average.add(3.0)
            assert average.get() == 2.0
            return
        lock = {'Lock': Lock, 'RLock': RLock, 'BoundedSemaphore': BoundedSemaphore}[recipe](cache, 'k')
        with lock:
            assert len(cache) == 1


def test_throttle_survives_its_bucket_expiring_or_being_cleared(tmp_path: Path) -> None:
    now = [1000.0]
    with Cache(tmp_path, clock=lambda: now[0]) as cache:

        @throttle(cache, 2, 1, expire=5, time_func=lambda: now[0], sleep_func=lambda s: now.__setitem__(0, now[0] + s))
        def work() -> int:
            return 1

        assert work() == 1
        now[0] += 60  # Idle past `expire`: diskcache raises TypeError unpacking None here.
        assert work() == 1
        cache.clear()
        assert work() == 1


def test_throttle_decoration_does_not_refill_a_shared_bucket(tmp_path: Path) -> None:
    now = [1000.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    with Cache(tmp_path) as cache:

        def make() -> Any:
            @throttle(cache, 1, 10, name='api', time_func=lambda: now[0], sleep_func=sleep)
            def call() -> int:
                return 1

            return call

        make()()  # Uses the one token.
        again = make()  # Another worker imports the module: diskcache refills the bucket here.
        again()
        assert sum(slept) == pytest.approx(10.0), 'the second call had to wait for a token'


def test_memoize_stampede_works_on_a_null_cache() -> None:
    calls = []

    @memoize_stampede(NullCache(), expire=60)
    def f(x: int) -> int:
        calls.append(x)
        return x

    assert f(1) == f(1) == 1
    assert calls == [1, 1]


def test_barrier_passes_no_tag_to_a_lock_factory_written_for_diskcache(tmp_path: Path) -> None:
    made = []

    def factory(cache: Any, key: Any, expire: Any = None) -> Lock:
        made.append(key)
        return Lock(cache, key, expire=expire)

    with Cache(tmp_path) as cache:

        @barrier(cache, factory, name='b')
        def work() -> int:
            return 2

        assert work() == 2
        assert made == ['b']


def _hold(directory: str, recipe: str, results: Any, rounds: int) -> None:
    with Cache(directory) as cache:
        lock = {'Lock': Lock, 'RLock': RLock, 'BoundedSemaphore': BoundedSemaphore}[recipe](cache, 'mutex')
        for _ in range(rounds):
            with lock:
                inside = cache.incr('inside')
                if inside > 1:
                    results.put('overlap')
                time.sleep(0.001)
                cache.decr('inside')
    results.put('done')


@pytest.mark.slow
@pytest.mark.parametrize('recipe', ['Lock', 'RLock', 'BoundedSemaphore'])
def test_mutual_exclusion_across_processes(tmp_path: Path, recipe: str) -> None:
    spawn = multiprocessing.get_context('spawn')
    results = spawn.Queue()
    procs = [spawn.Process(target=_hold, args=(str(tmp_path), recipe, results, 50)) for _ in range(4)]
    for p in procs:
        p.start()
    seen = [results.get(timeout=120) for _ in range(4)]
    for p in procs:
        p.join(60)
    assert seen == ['done'] * 4, seen


def test_an_rlock_held_by_the_parent_is_not_the_childs(tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-m', 'tests.unit.fork_scenarios', 'rlock-after-fork', str(tmp_path / 'c')],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
