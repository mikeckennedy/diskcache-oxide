"""SQLite's memory stays within the budget `max_connections` x `sqlite_cache_size` sets.

Measured with the bundled SQLite's own counters (`_core._sqlite_memory`), which cover every
connection in the process. Each test uses its own directory (a store's budget is per directory)
and measures growth from its own starting point, so caches other tests left open don't count.

Those counters exist only in the memory-instrumented build (SQLITE_DEFAULT_MEMSTATUS=1, the
`.venv-mem` environment scripts/check.sh builds). The shipped build turns them off for speed, so
there these tests skip, unless DCO_REQUIRE_MEMSTATUS=1 says this run is the instrumented one, in
which case they fail instead.
"""

from __future__ import annotations

import os
import random
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import pytest
from diskcache_oxide import Cache, _core


def _instrumented() -> bool:
    with tempfile.TemporaryDirectory() as tmp, Cache(tmp) as probe:
        probe.set('probe', 1)
        return _core._sqlite_memory()[0] > 0


if not _instrumented():
    if os.environ.get('DCO_REQUIRE_MEMSTATUS') == '1':
        raise RuntimeError('DCO_REQUIRE_MEMSTATUS=1, but this build has SQLite memory statistics off')
    pytest.skip('needs the memory-instrumented build (.venv-mem)', allow_module_level=True)

PAGE_SIZE = 4096  # SQLite's default; the format never changes it.
# What one cached page really costs: the page plus SQLite's per-page header and allocator rounding
# (measured: about 5,130 bytes).
PAGE_SLOT = PAGE_SIZE + 1280
DEFAULT_PAGES = 256  # store::DEFAULT_CACHE_PAGES
MAX_CONNECTIONS = 8  # store::DEFAULT_MAX_CONNECTIONS
# Per connection, beyond its page cache: schema, lookaside, cached statements (measured: about
# 330 KiB).
CONNECTION_OVERHEAD = 512 * 1024
KEYS = 20_000  # About 20 MB of rows: far more pages than any budget here.
BODY = 'x' * 1_000


def populated(directory: Path, **options: int) -> Cache:
    cache = Cache(directory, size_limit=2**34, **options)
    for i in range(KEYS):
        cache.set(f'k:{i}', {'id': i, 'body': BODY})
    return cache


def growth_while(work: Callable[[], object]) -> int:
    """How far SQLite's high-water mark rose above its starting point while `work()` ran."""
    start, _ = _core._sqlite_memory(reset=True)
    work()
    _, high = _core._sqlite_memory()
    return high - start


def populate_and_read(directory: Path, **options: int) -> Callable[[], Cache]:
    """Work for growth_while: open, fill and read back a cache, measured from before it opens."""

    def work() -> Cache:
        cache = populated(directory, **options)
        read_everything(cache)
        cache.close()
        return cache

    return work


def read_everything(cache: Cache) -> None:
    keys = [f'k:{i}' for i in range(KEYS)]
    random.Random(3).shuffle(keys)
    for key in keys:
        assert cache.get(key) is not None


def test_one_connection_stays_within_its_page_cache(tmp_path: Path) -> None:
    grew = growth_while(populate_and_read(tmp_path / 'c'))
    assert grew <= DEFAULT_PAGES * PAGE_SLOT + CONNECTION_OVERHEAD, f'{grew / 2**20:.1f} MiB'


def test_a_thread_storm_stays_within_the_stores_budget(tmp_path: Path) -> None:
    def storm() -> None:
        cache = populated(tmp_path / 'c')
        stop = time.perf_counter() + 1.5
        errors: list[BaseException] = []

        def worker(n: int) -> None:
            rng = random.Random(n)
            ops = 0
            try:
                while time.perf_counter() < stop:
                    key = f'k:{rng.randrange(KEYS)}'
                    if ops % 100 == 0:
                        cache.set(key, {'id': ops, 'body': BODY})
                    else:
                        cache.get(key)
                    ops += 1
            except BaseException as ex:
                errors.append(ex)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(32)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors, errors
        cache.close()

    grew = growth_while(storm)
    budget = MAX_CONNECTIONS * (DEFAULT_PAGES * PAGE_SLOT + CONNECTION_OVERHEAD)
    assert grew <= budget, f'{grew / 2**20:.1f} MiB > {budget / 2**20:.1f} MiB'


def test_sqlite_cache_size_raises_the_budget(tmp_path: Path) -> None:
    # Guards the guard: a larger setting really lets a connection use more, so the bounds above
    # come from the setting and not from a workload too small to fill the cache.
    pages = 4 * DEFAULT_PAGES
    grew = growth_while(populate_and_read(tmp_path / 'c', sqlite_cache_size=pages))
    assert grew > 2 * DEFAULT_PAGES * PAGE_SLOT + CONNECTION_OVERHEAD, f'{grew / 2**20:.1f} MiB'
    assert grew <= pages * PAGE_SLOT + CONNECTION_OVERHEAD, f'{grew / 2**20:.1f} MiB'
