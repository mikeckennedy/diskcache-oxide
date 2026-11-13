"""Value-file directories after large values go (plan 17).

diskcache removes a value file's directories when the file goes; oxide removes empty ones when the
cache's last handle closes, skipping any modified in the last 30 seconds, and after `clear()`
(D-042). Either way, once the directories are a minute old and the cache is closed, none is left
empty. The tests backdate the directories' mtimes to stand in for the minute.
"""

import os
import random
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import Backend
from tests.conformance.conftest import FakeClock
from tests.platforms import unix_only

KEYS = 12


def big(backend: Backend, seed: int) -> bytes:
    return random.Random(seed).randbytes(backend.spill_threshold + 1_000 + seed)


def subdirectories(directory: Path) -> list[Path]:
    return sorted(p for p in directory.rglob('*') if p.is_dir())


def empty_subdirectories(directory: Path) -> list[Path]:
    return [p for p in subdirectories(directory) if not any(p.iterdir())]


def backdate(directory: Path, seconds: float = 60.0) -> None:
    """Sets every subdirectory's mtime `seconds` back, as if nothing had touched it for that long."""
    then = time.time() - seconds
    for path in subdirectories(directory):
        os.utime(path, (then, then))


def fill(cache: Any, backend: Backend) -> None:
    for i in range(KEYS):
        cache.set(f'k{i}', big(backend, i))


def _delete(cache: Any, backend: Backend, clock: FakeClock) -> None:
    for i in range(KEYS):
        assert cache.delete(f'k{i}')


def _pop(cache: Any, backend: Backend, clock: FakeClock) -> None:
    for i in range(KEYS):
        assert cache.pop(f'k{i}') == big(backend, i)


def _overwrite_small(cache: Any, backend: Backend, clock: FakeClock) -> None:
    for i in range(KEYS):
        cache.set(f'k{i}', b'small')


def _overwrite_large(cache: Any, backend: Backend, clock: FakeClock) -> None:
    for i in range(KEYS):
        cache.set(f'k{i}', big(backend, 100 + i))
    for i in range(KEYS):
        assert cache.delete(f'k{i}')


def _add_on_existing(cache: Any, backend: Backend, clock: FakeClock) -> None:
    for i in range(KEYS):
        assert not cache.add(f'k{i}', big(backend, 200 + i))
    for i in range(KEYS):
        assert cache.delete(f'k{i}')


def _incr_over_expired(cache: Any, backend: Backend, clock: FakeClock) -> None:
    for i in range(KEYS):
        cache.set(f'k{i}', big(backend, 300 + i), expire=1)
    clock.advance(10)
    for i in range(KEYS):
        assert cache.incr(f'k{i}') == 1


def _expire(cache: Any, backend: Backend, clock: FakeClock) -> None:
    for i in range(KEYS):
        cache.set(f'k{i}', big(backend, 400 + i), expire=1)
    clock.advance(10)
    assert cache.expire() == KEYS


def _clear(cache: Any, backend: Backend, clock: FakeClock) -> None:
    assert cache.clear() == KEYS


def _held_commit(cache: Any, backend: Backend, clock: FakeClock) -> None:
    with cache.transact():
        for i in range(KEYS):
            cache.set(f'k{i}', b'small')


def _held_rollback(cache: Any, backend: Backend, clock: FakeClock) -> None:
    for i in range(KEYS):
        assert cache.delete(f'k{i}')
    with pytest.raises(ZeroDivisionError), cache.transact():
        cache.set('rolled-back', big(backend, 500))
        1 / 0  # noqa: B018


REMOVALS = {
    'delete': _delete,
    'pop': _pop,
    'overwrite-small': _overwrite_small,
    'overwrite-large': _overwrite_large,
    'add-on-existing': _add_on_existing,
    'incr-over-expired': _incr_over_expired,
    'expire': _expire,
    'clear': _clear,
    'held-commit': _held_commit,
    'held-rollback': _held_rollback,
}


@pytest.mark.parametrize('removal', REMOVALS)
def test_no_empty_value_directory_once_closed(
    make_cache: Callable[..., Any], cache_dir: Path, backend: Backend, fake_clock: FakeClock, removal: str
) -> None:
    cache = make_cache(cache_dir)
    fill(cache, backend)
    assert subdirectories(cache_dir), 'large values made value directories'
    REMOVALS[removal](cache, backend, fake_clock)
    backdate(cache_dir)
    cache.close()
    # A rolled-back transaction's file stays on diskcache (upstream/README.md), so its directory isn't
    # empty there: this holds on both, and the oxide-only test below asks for more.
    assert empty_subdirectories(cache_dir) == []


def test_no_empty_value_directory_after_culling(
    make_cache: Callable[..., Any], cache_dir: Path, backend: Backend
) -> None:
    cache = make_cache(cache_dir, size_limit=2**20)
    for i in range(40):
        cache.set(f'k{i}', big(backend, 600 + i))
    assert len(cache) < 40, 'set culls past size_limit'
    backdate(cache_dir)
    cache.close()
    assert empty_subdirectories(cache_dir) == []


@pytest.mark.backend_only('oxide')
@pytest.mark.covers('D-042')
def test_value_files_live_one_level_deep(make_cache: Callable[..., Any], cache_dir: Path, backend: Backend) -> None:
    cache = make_cache(cache_dir)
    fill(cache, backend)
    files = backend.spill_files(cache_dir)
    assert len(files) == KEYS
    for path in files:
        relative = path.relative_to(cache_dir)
        assert len(relative.parts) == 2, relative
        assert len(relative.parts[0]) == 2 and relative.parts[0] == relative.parts[0].lower(), relative
        assert int(relative.parts[0], 16) >= 0


def test_a_refused_add_of_a_large_value_leaves_no_file(
    make_cache: Callable[..., Any], cache_dir: Path, backend: Backend
) -> None:
    # The value is written to its file before the row is checked, so a refused add has a file to
    # take back: left behind, a claim that loses its race (add is how jobs are claimed) leaks one.
    cache = make_cache(cache_dir)
    fill(cache, backend)
    before = backend.spill_files(cache_dir)
    assert len(before) == KEYS
    for i in range(KEYS):
        assert not cache.add(f'k{i}', big(backend, 200 + i))
    assert backend.spill_files(cache_dir) == before
    cache.close()


@pytest.mark.backend_only('oxide')
@pytest.mark.covers('D-042')
def test_a_rolled_back_transaction_leaves_no_value_file(
    make_cache: Callable[..., Any], cache_dir: Path, backend: Backend
) -> None:
    cache = make_cache(cache_dir)
    with pytest.raises(ZeroDivisionError), cache.transact():
        cache.set('rolled-back', big(backend, 700))
        1 / 0  # noqa: B018
    assert backend.spill_files(cache_dir) == set()
    backdate(cache_dir)
    cache.close()
    assert subdirectories(cache_dir) == []


@pytest.mark.backend_only('oxide')
@pytest.mark.covers('D-042')
def test_clear_removes_the_directories_it_emptied_at_once(
    make_cache: Callable[..., Any], cache_dir: Path, backend: Backend
) -> None:
    cache = make_cache(cache_dir)
    fill(cache, backend)
    assert cache.clear() == KEYS
    assert subdirectories(cache_dir) == [], 'clear() sweeps with no age limit, before any close'


@pytest.mark.backend_only('oxide')
@pytest.mark.covers('D-042')
def test_close_leaves_recently_touched_directories(
    make_cache: Callable[..., Any], cache_dir: Path, backend: Backend
) -> None:
    cache = make_cache(cache_dir)
    fill(cache, backend)
    for i in range(KEYS):
        cache.delete(f'k{i}')
    cache.close()
    assert empty_subdirectories(cache_dir) == subdirectories(cache_dir) != [], 'young empties stay until a later close'
    backdate(cache_dir)
    make_cache(cache_dir).close()
    assert subdirectories(cache_dir) == []


@pytest.mark.backend_only('oxide')
@pytest.mark.covers('D-042')
@pytest.mark.parametrize('how', ['close', 'clear'])
def test_a_dead_writers_temp_file_goes_with_the_sweep(
    make_cache: Callable[..., Any], cache_dir: Path, backend: Backend, how: str
) -> None:
    """A `.tmp` nobody has written to for an hour is a killed writer's leftover (plan 19): the sweep
    removes it, once its folder has been quiet for 30 seconds on a close (at once on `clear()`). A
    young `.tmp` may be another process's write in flight, and a `.val` with no row may be a held
    transaction's file, so both stay."""
    cache = make_cache(cache_dir)
    cache.set('k', big(backend, 0))
    directory = next(p.parent for p in backend.spill_files(cache_dir))
    hex_name = directory.name + '0' * 30
    old_tmp = directory / f'{hex_name[:-1]}1.tmp'
    young_tmp = directory / f'{hex_name[:-1]}2.tmp'
    orphan_val = directory / f'{hex_name[:-1]}3.val'
    for path in (old_tmp, young_tmp, orphan_val):
        path.write_bytes(b'x')
    two_hours_ago = time.time() - 7_200
    for path in (old_tmp, orphan_val, directory):
        os.utime(path, (two_hours_ago, two_hours_ago))
    if how == 'close':
        cache.close()
    else:
        cache.clear()
    assert not old_tmp.exists(), 'the hour-old .tmp was swept'
    assert young_tmp.exists() and orphan_val.exists(), 'a young .tmp and any .val stay'


@unix_only('deleting an open cache directory, which Windows refuses while SQLite has the database open')
@pytest.mark.backend_only('oxide')
@pytest.mark.covers('D-043')
def test_a_large_write_into_a_deleted_cache_directory_fails_and_recreates_nothing(
    make_cache: Callable[..., Any], cache_dir: Path, backend: Backend
) -> None:
    cache = make_cache(cache_dir)
    cache.set('small', b'x')
    shutil.rmtree(cache_dir)
    with pytest.raises(OSError):
        cache.set('big', big(backend, 800))
    assert not cache_dir.exists(), 'the cache directory is never re-created'
