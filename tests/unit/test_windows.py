"""What deleting a cache directory does on Windows, and paths past MAX_PATH (plan 25, step 4).

On Windows a file can't be deleted while SQLite has it open (SQLite doesn't share delete), so a cache's
directory can't be removed while any handle on it is open, and `close()` must release every handle for
it to be (D-018). The checks after `close()` run everywhere.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
from diskcache_oxide import Cache, FanoutCache

BIG = 100_000  # spilled to a value file

windows_only = pytest.mark.skipif(sys.platform != 'win32', reason='Windows only: deleting an open file')


@pytest.mark.parametrize('fanout', [False, True])
def test_a_closed_cache_directory_can_be_removed_and_made_again(tmp_path: Path, fanout: bool) -> None:
    directory = tmp_path / 'c'
    cache = FanoutCache(directory, shards=4) if fanout else Cache(directory)
    for i in range(20):
        cache.set(f'k{i}', b'x' * BIG if i % 2 else i)
    assert cache.get('k1') == b'x' * BIG
    cache.close()
    shutil.rmtree(directory)  # nothing left open, so this works on Windows too
    again = FanoutCache(directory, shards=4) if fanout else Cache(directory)
    assert again.get('k1') is None
    again.set('k1', 1)
    assert again.get('k1') == 1
    again.close()
    shutil.rmtree(directory)


@windows_only
def test_an_open_cache_directory_cant_be_removed_and_the_cache_keeps_working(tmp_path: Path) -> None:
    directory = tmp_path / 'c'
    cache = Cache(directory)
    cache.set('small', 1)
    cache.set('big', b'x' * BIG)
    with pytest.raises(PermissionError):
        shutil.rmtree(directory)
    # rmtree removed what nothing held open (the value file) and stopped at the database.
    assert cache.get('small') == 1
    assert cache.get('big') is None, 'its value file was removed: a miss, and the row goes (D-013)'
    assert 'big' not in cache
    cache.set('big', b'y' * BIG)
    assert cache.get('big') == b'y' * BIG
    cache.close()
    shutil.rmtree(directory)


# Only Windows, through the win32-longpath VFS, takes 2,000: SQLite's unix VFS refuses any path past
# its 512-byte mxPathname, and macOS can't even create one past its 1,024-byte PATH_MAX.
past_unix_path_limits = pytest.mark.skipif(sys.platform != 'win32', reason="past SQLite's 512-byte unix mxPathname")


@pytest.mark.parametrize('length', [300, pytest.param(2_000, marks=past_unix_path_limits)])
@pytest.mark.parametrize('fanout', [False, True])
def test_a_cache_past_max_path_works(tmp_path: Path, fanout: bool, length: int) -> None:
    # The core's own test covers a process that isn't long-path aware; python.exe is, so this one
    # checks the whole stack, value files and FanoutCache's shard paths included.
    directory = tmp_path
    while len(str(directory / '00' / 'diskcache-oxide.sqlite-journal')) < length:
        directory = directory / ('d' * 40)
    cache = FanoutCache(directory, shards=2) if fanout else Cache(directory)
    cache.set('small', 1)
    cache.set('big', b'x' * BIG)
    assert cache.get('small') == 1 and cache.get('big') == b'x' * BIG
    assert cache.check() == []
    cache.close()
