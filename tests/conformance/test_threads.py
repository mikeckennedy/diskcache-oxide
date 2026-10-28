"""Thread safety. Most meaningful on 3.14t (free-threaded), where the threads really run in parallel.

Each scenario runs twice: every thread sharing one Cache object, and one Cache per thread on the
same directory. Counts were calibrated on 3.14t (16 threads x 1000 ops is about 0.5 s per scenario).
"""

from __future__ import annotations

import threading
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import Backend

THREADS = 16
INCR_OPS = 1000
ADD_ROUNDS = 300
MIXED_OPS = 1000
MIXED_KEYS = 24
JOIN_TIMEOUT = 30.0

pytestmark = pytest.mark.parametrize('layout', ['shared', 'per-thread'])


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


def test_incr_total_is_exact(layout: str, make_cache: Callable[..., Any], cache_dir: Path) -> None:
    caches = _caches(layout, make_cache, cache_dir)

    def work(tid: int, cache: Any) -> None:
        for _ in range(INCR_OPS):
            cache.incr('counter')

    _run(work, caches)
    assert caches[0].get('counter') == THREADS * INCR_OPS


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


def _mixed_value(key: str, tid: int, n: int, spill_size: int) -> str:
    # Every 10th value is twice the spill threshold, so it lands in a value file rather than the database row.
    body = f'{key}|{tid}|{n}|' + ('x' * spill_size if n % 10 == 0 else '')
    return f'{body}|{zlib.crc32(body.encode()):08x}'


def _check_mixed_value(key: str, value: Any) -> None:
    assert isinstance(value, str), f'{key}: {type(value)}'
    body, _, checksum = value.rpartition('|')
    assert body.split('|', 1)[0] == key, f'{key}: value belongs to another key'
    assert f'{zlib.crc32(body.encode()):08x}' == checksum, f'{key}: checksum mismatch'


def test_mixed_get_set_delete_never_corrupts(layout: str, make_cache: Callable[..., Any], cache_dir: Path, backend: Backend) -> None:
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
