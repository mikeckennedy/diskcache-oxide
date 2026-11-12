"""The coordination recipes (plan 24, release 2), against diskcache 5.6.3's `recipes.py`.

Single-process behavior here; cross-process mutual exclusion is in the oxide unit tests.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from tests.conformance.backends import Backend

pytestmark = pytest.mark.requires('recipes')


def test_averager_keeps_a_running_average(cache: Any, backend: Backend) -> None:
    average = backend.recipe('Averager')(cache, 'latency')
    assert average.get() is None
    average.add(0.080)
    average.add(0.120)
    assert average.get() == pytest.approx(0.1)
    average.add(0.160)
    assert average.pop() == pytest.approx(0.12)
    assert average.get() is None


def test_a_lock_is_held_until_released(cache: Any, backend: Backend) -> None:
    lock = backend.recipe('Lock')(cache, 'report')
    assert not lock.locked()
    lock.acquire()
    assert lock.locked()
    other = backend.recipe('Lock')(cache, 'report')
    got = threading.Event()

    def contender() -> None:
        other.acquire()
        got.set()
        other.release()

    thread = threading.Thread(target=contender)
    thread.start()
    assert not got.wait(0.2), 'a second holder got the lock'
    lock.release()
    assert got.wait(5)
    thread.join(5)
    with lock:
        assert lock.locked()
    assert not lock.locked()


def test_an_rlock_is_reentrant_and_refuses_an_extra_release(cache: Any, backend: Backend) -> None:
    rlock = backend.recipe('RLock')(cache, 'user')
    rlock.acquire()
    rlock.acquire()
    rlock.release()
    with rlock:
        pass
    rlock.release()
    with pytest.raises(AssertionError, match='cannot release un-acquired lock'):
        rlock.release()


def test_an_rlock_held_by_another_thread_waits(cache: Any, backend: Backend) -> None:
    rlock = backend.recipe('RLock')(cache, 'user')
    held, done = threading.Event(), threading.Event()

    def owner() -> None:
        with rlock:
            held.set()
            done.wait(5)

    thread = threading.Thread(target=owner)
    thread.start()
    assert held.wait(5)
    got = threading.Event()

    def contender() -> None:
        with rlock:
            got.set()

    other = threading.Thread(target=contender)
    other.start()
    assert not got.wait(0.2), 'two threads held the rlock'
    done.set()
    assert got.wait(5)
    thread.join(5)
    other.join(5)


def test_a_bounded_semaphore_admits_its_value(cache: Any, backend: Backend) -> None:
    semaphore = backend.recipe('BoundedSemaphore')(cache, 'cons', value=2)
    semaphore.acquire()
    semaphore.acquire()
    got = threading.Event()

    def third() -> None:
        semaphore.acquire()
        got.set()

    thread = threading.Thread(target=third)
    thread.start()
    assert not got.wait(0.2), 'a third holder got in'
    semaphore.release()
    assert got.wait(5)
    thread.join(5)
    semaphore.release()
    semaphore.release()
    with pytest.raises(AssertionError, match='cannot release un-acquired semaphore'):
        semaphore.release()


def test_throttle_limits_calls_per_period(cache: Any, backend: Backend) -> None:
    now = [1000.0]
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    @backend.recipe('throttle')(cache, 2, 1, time_func=lambda: now[0], sleep_func=sleep)
    def work() -> int:
        return 1

    assert sum(work() for _ in range(6)) == 6
    # Two calls a second: the bucket starts full (2), then each call waits half a second.
    assert sum(slept) == pytest.approx(2.0, abs=0.01)


def test_barrier_runs_one_caller_at_a_time(cache: Any, backend: Backend) -> None:
    inside = []
    overlap = []

    @backend.recipe('barrier')(cache, backend.recipe('Lock'))
    def work() -> None:
        inside.append(1)
        if len(inside) > 1:
            overlap.append(1)
        time.sleep(0.02)
        inside.pop()

    threads = [threading.Thread(target=work) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not overlap


def test_memoize_stampede_caches_and_keys_like_memoize(cache: Any, backend: Backend) -> None:
    calls = []

    @backend.recipe('memoize_stampede')(cache, expire=60)
    def square(n: int) -> int:
        calls.append(n)
        return n * n

    assert square(4) == 16
    assert square(4) == 16
    assert calls == [4]
    key = square.__cache_key__(4)
    assert key[1:] == (4, None)
    value, seconds = cache.get(key)
    assert value == 16 and seconds >= 0


@pytest.mark.diverges(
    backend='oxide', ref='D-051', reason='the owner carries a per-process nonce', raises=AssertionError
)
def test_an_rlock_stores_its_owner_as_pid_and_thread(cache: Any, backend: Backend) -> None:
    import os

    rlock = backend.recipe('RLock')(cache, 'owned')
    rlock.acquire()
    assert cache.get('owned') == (f'{os.getpid()}-{threading.get_ident()}', 1)
    rlock.release()
