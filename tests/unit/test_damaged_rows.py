"""A row with a column of the wrong SQLite type is a miss, never an error, and `check` finds it (plan 32, D1).

One such row (a damaged page, or a hand that edited the file) used to make every `get`, `delete`,
`clear()` and `check(fix=True)` that reached it raise `OSError: Invalid column type`, and the row
stayed, so once it was the oldest every `set` that had to evict raised too. D-013 promises a damaged
row reads as a miss.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from diskcache_oxide import Cache

BIG = b'x' * 200_000  # Past the spill threshold: stored in a value file, so the row has a filename.


def sql(directory: Path, statement: str) -> None:
    """Runs `statement` on the cache's database from another process (never a second SQLite in this one)."""
    script = (
        'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\ncon.execute(sys.argv[2])\ncon.commit()\ncon.close()'
    )
    database = directory / 'diskcache-oxide.sqlite'
    subprocess.run([sys.executable, '-c', script, str(database), statement], check=True, timeout=30)


def damaged(directory: Path, column: str, value: str) -> Cache:
    """A cache with a spilled value under 'k' whose row has `column` set to `value`, and 'a' untouched."""
    with Cache(directory) as cache:
        cache.set('k', BIG)
        cache.set('a', 'inline')
    sql(directory, f'UPDATE cache SET {column} = {value} WHERE filename IS NOT NULL')
    return Cache(directory)


FILE_COLUMNS = [
    ('filename', "x'fffe'"),
    ('filename', '42'),
    ('filename', "CAST(x'fffe' AS TEXT)"),  # Text that isn't UTF-8, as a flipped bit in a name leaves it.
    ('size', "'big'"),
    ('size', '1.5'),
]
ids = [f'{column}={value}' for column, value in FILE_COLUMNS]


@pytest.mark.parametrize(('column', 'value'), FILE_COLUMNS, ids=ids)
def test_a_damaged_filename_or_size_is_a_miss(tmp_path: Path, column: str, value: str) -> None:
    with damaged(tmp_path / 'c', column, value) as cache:
        assert cache.get('k', 'default') == 'default'
        assert 'k' not in cache
        with pytest.raises(KeyError):
            cache['k']
        assert cache.get('a') == 'inline'
        assert cache.delete('k') is False, 'the miss removed the damaged row'
        assert len(cache) == 1


@pytest.mark.parametrize(('column', 'value'), FILE_COLUMNS, ids=ids)
def test_a_pop_of_a_damaged_row_is_a_miss_and_removes_it(tmp_path: Path, column: str, value: str) -> None:
    # Its own cache: pop reads the row under the write lock, another path than get's.
    with damaged(tmp_path / 'c', column, value) as cache:
        assert cache.pop('k', 'default') == 'default'
        assert sorted(str(key) for key in cache) == ['a']
        assert cache.check() == [] or column == 'size', 'only a damaged size leaves the size total off'


@pytest.mark.parametrize(('column', 'value'), FILE_COLUMNS, ids=ids)
def test_a_damaged_filename_or_size_doesnt_stop_a_delete_an_overwrite_or_a_clear(
    tmp_path: Path, column: str, value: str
) -> None:
    with damaged(tmp_path / 'delete', column, value) as cache:
        assert cache.delete('k') is True
        assert list(cache) == ['a']
    with damaged(tmp_path / 'set', column, value) as cache:
        assert cache.set('k', 'new') is True
        assert cache.get('k') == 'new'
    with damaged(tmp_path / 'clear', column, value) as cache:
        assert cache.clear() == 2
        assert len(cache) == 0
    with damaged(tmp_path / 'iterate', column, value) as cache:
        assert sorted(str(key) for key in cache) == ['a', 'k']
        assert len(cache) == 2 and cache.volume() > 0


def test_a_damaged_row_that_is_the_oldest_is_evicted_like_any_other(tmp_path: Path) -> None:
    with damaged(tmp_path / 'c', 'filename', "x'fffe'") as cache:
        cache.reset('size_limit', 2**20)
        for n in range(12):
            assert cache.set(f'fill{n}', BIG) is True, 'a set that had to evict raised on the damaged row'
        assert 'k' not in cache and 'a' not in cache, 'the two oldest rows were evicted'


WRONG_TYPES = [
    ('filename', "x'fffe'", 'blob'),
    ('size', "'big'", 'text'),
    ('mode', "'two'", 'text'),
    ('store_seq', "x'00'", 'blob'),
    ('expire_time', "'soon'", 'text'),
]


@pytest.mark.parametrize(('column', 'value', 'kind'), WRONG_TYPES, ids=[c for c, _, _ in WRONG_TYPES])
def test_check_reports_a_wrong_typed_column_and_fix_removes_the_row(
    tmp_path: Path, column: str, value: str, kind: str
) -> None:
    with damaged(tmp_path / 'c', column, value) as cache:
        text = [str(w.message) for w in cache.check()]
        assert any(f'its {column} is stored as {kind}' in line for line in text), text
        assert len(cache) == 2, 'a check without fix removes nothing'
        assert [str(w.message) for w in cache.check()] == text, 'and finds the same again'

        cache.check(fix=True)
        assert len(cache) == 1 and cache.get('a') == 'inline' and 'k' not in cache
        assert cache.check() == []
        files = [p for p in (tmp_path / 'c').rglob('*.val')]
        assert column == 'filename' or files == [], 'a row removed by fix takes its value file with it'


def test_check_reports_a_filename_that_isnt_utf8_and_fix_removes_the_row(tmp_path: Path) -> None:
    with damaged(tmp_path / 'c', 'filename', "CAST(x'fffe' AS TEXT)") as cache:
        text = [str(w.message) for w in cache.check()]
        assert any('damaged file name in a row' in line for line in text), text
        cache.check(fix=True)
        assert len(cache) == 1 and cache.check() == []
