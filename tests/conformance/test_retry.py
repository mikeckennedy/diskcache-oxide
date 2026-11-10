"""`retry=` (plan 24, release 1), against diskcache 5.6.3.

With `retry=True` a call that meets a busy database keeps trying past its `timeout` until it gets
through; without it, the call raises `Timeout`. Another process holds SQLite's write lock (see
test_locking.py for why it's a subprocess).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import Backend
from tests.conformance.proc_workers import SqlitePeer

pytestmark = pytest.mark.requires('retry')

TIMEOUT = 0.05
HOLD = 0.4

CALLS: dict[str, Callable[[Any], Any]] = {
    'set': lambda c: c.set('k', 'new', retry=True),
    'add': lambda c: c.add('fresh', 1, retry=True),
    'incr': lambda c: c.incr('n', retry=True),
    'decr': lambda c: c.decr('n', retry=True),
    'delete': lambda c: c.delete('k', retry=True),
    'touch': lambda c: c.touch('k', expire=60, retry=True),
    'pop': lambda c: c.pop('k', retry=True),
}


@pytest.fixture
def prepared(make_cache: Callable[..., Any], cache_dir: Path) -> Any:
    cache = make_cache(cache_dir, timeout=TIMEOUT)
    cache.set('k', 'v')
    cache.set('n', 1)
    return cache


@pytest.mark.parametrize('call', CALLS.values(), ids=CALLS.keys())
def test_a_retried_write_waits_out_another_processes_lock(
    prepared: Any, backend: Backend, cache_dir: Path, call: Callable[[Any], Any]
) -> None:
    with SqlitePeer(backend.db_path(cache_dir)) as peer:
        peer.begin('IMMEDIATE')
        with pytest.raises(backend.timeout_error):
            prepared.set('k', 'blocked')
        release = threading.Timer(HOLD, peer.rollback)
        release.start()
        start = time.monotonic()
        try:
            call(prepared)
        finally:
            release.join()
        assert time.monotonic() - start >= HOLD * 0.9, 'it got through while the lock was held'


@pytest.mark.parametrize(
    'call',
    [
        lambda c: c.get('k', retry=True),
        lambda c: c.set('k', 'v', retry=False),
        lambda c: c.add('a', 1, retry=False),
        lambda c: c.incr('n', retry=False),
        lambda c: c.delete('missing', retry=False),
        lambda c: c.touch('k', retry=False),
        lambda c: c.pop('missing', retry=False),
        lambda c: c.expire(retry=True),
        lambda c: c.cull(retry=True),
        lambda c: c.clear(retry=True),
    ],
)
def test_every_call_takes_retry(prepared: Any, call: Callable[[Any], Any]) -> None:
    call(prepared)


@pytest.mark.diverges(backend='oxide', ref='D-047', reason='retry is keyword-only', raises=TypeError)
def test_retry_can_be_passed_by_position(prepared: Any) -> None:
    # diskcache: set(key, value, expire, read, tag, retry).
    assert prepared.set('k', 'v', None, False, None, True) is True
