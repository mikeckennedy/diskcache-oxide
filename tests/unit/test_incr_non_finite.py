"""`incr` and `decr` refuse an infinity or a NaN on every kind of handle (D-064, plan 32).

The conformance suite has the plain `Cache` side (tests/conformance/test_add_incr.py). These are the
handles it doesn't build: a `FanoutCache`, a durable one of each, and inside a transaction.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache, FanoutCache

NON_FINITE = [float('inf'), float('-inf'), float('nan')]
HANDLES: dict[str, Callable[[Path], Any]] = {
    'Cache': lambda d: Cache(d),
    'FanoutCache': lambda d: FanoutCache(d, shards=2),
    'durable Cache': lambda d: Cache(d, durable=True),
    'durable FanoutCache': lambda d: FanoutCache(d, shards=2, durable=True),
}


@pytest.mark.parametrize('bad', NON_FINITE, ids=['inf', '-inf', 'nan'])
@pytest.mark.parametrize('kind', HANDLES)
def test_a_non_finite_delta_or_default_raises_and_changes_nothing(kind: str, bad: float, tmp_path: Path) -> None:
    with HANDLES[kind](tmp_path / 'c') as cache:
        cache.set('k', 2)
        calls: list[Callable[[], object]] = [
            lambda: cache.incr('k', bad),
            lambda: cache.decr('k', bad),
            lambda: cache.incr('k', 1, default=bad),
            lambda: cache.incr('fresh', bad),
            lambda: cache.decr('fresh', 1, default=bad),
        ]
        for call in calls:
            # A wrong argument, never an outage: a durable handle raises it too.
            with pytest.raises(ValueError, match='must be a finite number'):
                call()
        assert cache.get('k') == 2
        assert 'fresh' not in cache
        assert cache.incr('k', 0.5) == 2.5, 'a finite float still works'


def test_it_is_refused_inside_a_transaction_and_the_transaction_still_commits(tmp_path: Path) -> None:
    with Cache(tmp_path / 'c') as cache:
        with cache.transact():
            cache.set('k', 1)
            with pytest.raises(ValueError, match='delta must be a finite number, not NaN'):
                cache.incr('k', math.nan)
            with pytest.raises(ValueError, match='default must be a finite number, not inf'):
                cache.incr('other', 1, default=math.inf)
            assert cache.incr('k') == 2
        assert cache.get('k') == 2 and 'other' not in cache
