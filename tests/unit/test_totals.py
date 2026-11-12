"""The `totals` row (entry count and bytes in value files) stays equal to the table it summarizes.

`volume()` and eviction read `totals`, not the table, so a removal that forgets to subtract what it
removed makes the cache look fuller for good. Checked here with values on both sides of the spill
threshold, since only a spilled value has a size to forget, after each way an entry can leave.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from diskcache_oxide import Cache

SPILL = 64 * 1024
MiB = 1 << 20
QUERY = (
    'import sqlite3, sys\n'
    'con = sqlite3.connect(sys.argv[1])\n'
    'print(*con.execute("SELECT (SELECT count FROM state), (SELECT size FROM state),'
    ' (SELECT COUNT(*) FROM cache), (SELECT COALESCE(SUM(size), 0) FROM cache)").fetchone())\n'
)


def totals_and_table(directory: Path) -> tuple[tuple[int, int], tuple[int, int]]:
    """`(count, size)` from `totals` and from the table, read by another process's SQLite (the
    stdlib's in this one would break the bundled SQLite's POSIX locks)."""
    out = subprocess.run(
        [sys.executable, '-c', QUERY, str(directory / 'diskcache-oxide.sqlite')],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    count, size, rows, row_bytes = (int(x) for x in out)
    return (count, size), (rows, row_bytes)


def value(i: int) -> bytes:
    # Every third value is inline; the rest spill, at different lengths.
    return bytes([i % 251]) * (1_000 if i % 3 == 0 else SPILL + 1_000 * i)


def _clear(cache: Cache, now: list[float]) -> None:
    cache.clear()


def _expire(cache: Cache, now: list[float]) -> None:
    now[0] += 1_000
    cache.expire()


def _sweep(cache: Cache, now: list[float]) -> None:
    now[0] += 1_000
    for i in range(6):
        cache.set(f'after{i}', value(i + 1))


def _delete_and_pop(cache: Cache, now: list[float]) -> None:
    for i in range(0, 30, 2):
        assert cache.delete(f'k{i}')
    for i in range(1, 30, 2):
        assert cache.pop(f'k{i}') == value(i)


def _overwrite(cache: Cache, now: list[float]) -> None:
    for i in range(30):
        cache.set(f'k{i}', value(i + 1))  # Inline over spilled, spilled over inline, spilled over spilled.


def _refused_add(cache: Cache, now: list[float]) -> None:
    for i in range(30):
        assert not cache.add(f'k{i}', value(i + 1))


def _cull(cache: Cache, now: list[float]) -> None:
    for i in range(40):
        cache.set(f'more{i}', bytes([i]) * (SPILL + 50_000))
    assert cache.cull() >= 0
    assert cache.volume() <= cache.size_limit


def _rolled_back(cache: Cache, now: list[float]) -> None:
    with pytest.raises(ZeroDivisionError), cache.transact():
        for i in range(30):
            cache.set(f'k{i}', value(i + 2))
        cache.delete('k1')
        1 / 0  # noqa: B018


REMOVALS: dict[str, Callable[[Cache, list[float]], None]] = {
    'clear': _clear,
    'expire': _expire,
    'sweep-on-set': _sweep,
    'delete-and-pop': _delete_and_pop,
    'overwrite': _overwrite,
    'refused-add': _refused_add,
    'cull-under-size-limit': _cull,
    'rolled-back-transaction': _rolled_back,
}


@pytest.mark.parametrize('removal', REMOVALS)
def test_totals_match_the_table(tmp_path: Path, removal: str) -> None:
    now = [1_000_000.0]
    cache = Cache(tmp_path / 'c', size_limit=3 * MiB, clock=lambda: now[0])
    for i in range(30):
        cache.set(f'k{i}', value(i), expire=500)
    totals, table = totals_and_table(tmp_path / 'c')
    assert totals == table and totals[1] > 20 * SPILL, 'spilled values are counted'

    REMOVALS[removal](cache, now)

    totals, table = totals_and_table(tmp_path / 'c')
    assert totals == table, f'after {removal}: totals (count, size) {totals}, the table {table}'
    files = sum(p.stat().st_size for p in (tmp_path / 'c').rglob('*.val'))
    assert files == table[1], f'after {removal}: {files} bytes of value files, {table[1]} in the table'
    cache.close()
