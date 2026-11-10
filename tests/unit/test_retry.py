"""`retry=True`, oxide-only (plan 24, release 1).

The conformance suite (`tests/conformance/test_retry.py`) checks the diskcache behavior: a retried
write waits out another process's lock. These check what is ours: a durable handle retries a
`Timeout` instead of answering it as a miss, `retry` is a strict bool, Ctrl-C ends a retry, a
`timeout=0` retry doesn't spin, and a retried removal returns everything it removed, on `Cache` and
across a `FanoutCache`'s shards.
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache, FanoutCache, NullCache, Timeout

from tests.conformance.proc_workers import SqlitePeer

HOLD = 0.4
DB = 'diskcache-oxide.sqlite'


@contextmanager
def locked_for(db: Path, seconds: float) -> Generator[None]:
    """Another process holds `db`'s write lock for `seconds` from now."""
    with SqlitePeer(db) as peer:
        peer.begin('IMMEDIATE')
        release = threading.Timer(seconds, peer.rollback)
        release.start()
        try:
            yield
        finally:
            release.join()


def test_a_durable_handle_retries_a_timeout_instead_of_missing(tmp_path: Path) -> None:
    with Cache(tmp_path, timeout=0.05, durable=True) as cache:
        cache.set('k', 'old')
        with locked_for(tmp_path / DB, HOLD):
            assert cache.set('k', 'skipped') is False, 'without retry, durable drops the write'
            start = time.monotonic()
            assert cache.set('k', 'new', retry=True) is True
            assert time.monotonic() - start >= HOLD * 0.8
        assert cache.get('k') == 'new'


@pytest.mark.parametrize('make', [lambda p: Cache(p), lambda p: FanoutCache(p, shards=2), lambda p: NullCache()])
def test_retry_must_be_a_bool(tmp_path: Path, make: Any) -> None:
    cache = make(tmp_path)
    for call in (
        lambda: cache.set('k', 1, retry=1),
        lambda: cache.get('k', retry='yes'),
        lambda: cache.delete('k', retry=None),
        lambda: cache.clear(retry=1),
    ):
        with pytest.raises(TypeError):
            call()
    cache.close()


class Interrupted(Exception):
    pass


def test_a_signal_ends_a_retry_that_would_never_get_through(tmp_path: Path) -> None:
    def interrupt(signum: int, frame: Any) -> None:
        raise Interrupted

    previous = signal.signal(signal.SIGUSR1, interrupt)
    try:
        with Cache(tmp_path, timeout=0.05) as cache, SqlitePeer(tmp_path / DB) as peer:
            cache.set('k', 'v')
            peer.begin('IMMEDIATE')
            timer = threading.Timer(0.3, signal.raise_signal, (signal.SIGUSR1,))
            timer.start()
            start = time.monotonic()
            with pytest.raises(Interrupted):
                cache.set('k', 'never', retry=True)
            assert time.monotonic() - start < 5, 'the signal ended the retry'
            timer.join()
            peer.rollback()
    finally:
        signal.signal(signal.SIGUSR1, previous)


def test_a_retry_with_no_timeout_does_not_spin(tmp_path: Path) -> None:
    with Cache(tmp_path, timeout=0) as cache:
        cache.set('k', 'v')
        with locked_for(tmp_path / DB, HOLD):
            wall, cpu = time.monotonic(), time.process_time()
            cache.set('k', 'new', retry=True)
            wall, cpu = time.monotonic() - wall, time.process_time() - cpu
        assert cpu < 0.5 * wall, f'{cpu:.3f}s of CPU in {wall:.3f}s: the retry spun'


def test_a_retried_clear_returns_every_entry_it_removed(tmp_path: Path) -> None:
    with Cache(tmp_path, timeout=0.05) as cache:
        for i in range(50):
            cache.set(f'k{i}', i)
        with locked_for(tmp_path / DB, HOLD):
            assert cache.clear(retry=True) == 50
        assert len(cache) == 0


def test_a_retried_fanout_clear_adds_up_every_shard_and_every_round(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=4, timeout=0.05) as cache:
        for i in range(80):
            cache.set(f'k{i}', i)
        # One shard is locked by another process for a while; the others clear at once.
        locked = cache.shard_of('k0')
        db = next(tmp_path.glob(f'*{locked:03d}*')) / DB
        assert db.exists(), db
        with locked_for(db, HOLD):
            with pytest.raises(Timeout) as raised:
                cache.clear()
            first = raised.value.removed  # ty: ignore[unresolved-attribute]
            assert first < 80
            assert cache.clear(retry=True) == 80 - first
        assert len(cache) == 0


def test_a_null_cache_passes_retry_on_to_its_invalidate_cache(tmp_path: Path) -> None:
    with Cache(tmp_path, timeout=0.05) as real:
        real.set('k', 1)
        null = NullCache(invalidate=real)
        with locked_for(tmp_path / DB, HOLD):
            assert null.delete('k', retry=True) is True
        assert 'k' not in real
