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
    ('store_seq', '9223372036854775807', 'an integer too large to be one'),
    ('store_seq', '4611686018427387903', 'an integer too large to be one'),  # The last one: nothing can follow it.
    ('size', '-9223372036854775807', 'an integer no size can be'),
    ('size', '9223372036854775807', 'an integer no size can be'),
    ('expire_time', "'soon'", 'text'),
]


@pytest.mark.parametrize(('column', 'value', 'kind'), WRONG_TYPES, ids=[f'{c}={v}' for c, v, _ in WRONG_TYPES])
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


# A damaged `store_seq`, and whether `clear()` reaches that row: only one that still compares below
# the bound, a REAL inside the range.
DAMAGED_SEQS = [
    ("'abc'", 4),
    ("x'00'", 4),
    ('1e300', 4),
    ('1500.5', 5),
    ('9223372036854775807', 4),
    ('4611686018427387904', 4),
]


@pytest.mark.parametrize(('value', 'cleared'), DAMAGED_SEQS, ids=[value for value, _ in DAMAGED_SEQS])
def test_a_damaged_store_seq_gives_no_other_key_a_repeated_number(tmp_path: Path, value: str, cleared: int) -> None:
    """Plan 32, step 8. One row's damaged `store_seq` used to decide every later sequence number: text or
    a BLOB gave each later write the same one, so `clear()` returned 0 and left rows behind, and a large
    REAL made every `set` on any key raise."""
    with damaged(tmp_path / 'c', 'store_seq', value) as cache:
        assert cache.set('b', 1) is True
        assert cache.set('c', 2) is True
        assert cache.incr('n') == 1
        assert cache.incr('n') == 2
        assert (cache.get('a'), cache.get('b'), cache.get('c')) == ('inline', 1, 2)
        first = cache.push('x')
        second = cache.push('y')
        assert cache.pull() == (first, 'x')
        assert cache.pull() == (second, 'y')
        assert cache.pull() == (None, None), 'each pushed value came back exactly once'
        assert cache.expire() == 0
        assert cache.cull() == 0
        assert cache.set('a', 'again') is True, 'an overwrite'
        # The entries stored before it started all go: 'a', 'b', 'c' and 'n'.
        assert cache.clear() == cleared
        assert [key for key in ('a', 'b', 'c', 'n') if key in cache] == []
        assert cache.set('d', 1) is True
        assert len(cache) == 1 + (5 - cleared), 'the damaged row is left for check, where clear() could not reach it'
        cache.check(fix=True)
        assert cache.check() == []
        assert cache.get('d') == 1 and len(cache) == 1


DAMAGED_STATE = [
    ('next_seq', "'abc'", 'text'),
    ('next_seq', '1500.5', 'real'),
    ('next_seq', '4611686018427387904', 'an integer too large to be one'),
    ('size', '9.3e18', 'real'),
    ('count', "x'00'", 'blob'),
]


@pytest.mark.parametrize(('column', 'value', 'kind'), DAMAGED_STATE, ids=[f'{c}={v}' for c, v, _ in DAMAGED_STATE])
def test_check_reports_a_damaged_total_or_next_number_and_fix_repairs_it(
    tmp_path: Path, column: str, value: str, kind: str
) -> None:
    # Gemini's review of plan 32, M3 and M4: `check` raised on a total of another type, said nothing
    # of a damaged next sequence number (every write then fails), and repaired neither.
    directory = tmp_path / 'c'
    with Cache(directory) as cache:
        cache.set('k', BIG)
        cache.set('n', 1)
    sql(directory, f'UPDATE state SET {column} = {value}')
    with Cache(directory) as cache:
        assert cache.get('n') == 1, 'reads go on'
        if column == 'next_seq':
            # Loudly, and never with a number another row has, or one that isn't an integer.
            with pytest.raises(OSError):
                cache.set('new', 1)
            with pytest.raises(OSError):
                cache.incr('n')
            assert cache.get('n') == 1
        text = [str(w.message) for w in cache.check()]
        assert text == [f'damaged cache state: its {column} is stored as {kind}'], text
        cache.check(fix=True)
        assert cache.check() == []
        assert cache.set('new', 1) is True and cache.incr('n') == 2
        assert len(cache) == 3 and cache.volume() > len(BIG)
        assert cache.clear() == 3


def test_clear_still_empties_a_cache_whose_next_number_is_damaged(tmp_path: Path) -> None:
    directory = tmp_path / 'c'
    with Cache(directory) as cache:
        cache.set('k', BIG)
        cache.set('a', 'inline', tag='t')
    sql(directory, "UPDATE state SET next_seq = 'abc'")
    with Cache(directory) as cache:
        assert cache.evict('t') == 1
        assert cache.clear() == 1
        assert len(cache) == 0
