"""Core get/set/delete semantics, the mapping protocol, lifecycle, and settings persistence."""

import hashlib
import os
import stat
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.conformance.backends import Backend
from tests.conformance.proc_workers import SqlitePeer, checkpoint

NON_STR_KEYS = [
    pytest.param(1, id='int'),
    pytest.param(1.5, id='float'),
    pytest.param(b'bytes', id='bytes'),
    pytest.param((1, 2), id='tuple'),
    pytest.param(None, id='none'),
]


# ---------------------------------------------------------------------------
# get / set / delete
# ---------------------------------------------------------------------------


def test_get_missing_returns_none(cache: Any) -> None:
    assert cache.get('missing') is None


def test_get_missing_returns_given_default(cache: Any) -> None:
    assert cache.get('missing', 5) == 5
    assert cache.get('missing', default='d') == 'd'


def test_get_missing_with_expire_time_returns_default_pair(cache: Any) -> None:
    assert cache.get('missing', expire_time=True) == (None, None)
    assert cache.get('missing', default=3, expire_time=True) == (3, None)


def test_set_returns_true_and_round_trips(cache: Any) -> None:
    assert cache.set('k', 'v') is True
    assert cache.get('k') == 'v'


def test_set_overwrites(cache: Any) -> None:
    cache.set('k', 'v1')
    assert cache.set('k', 'v2') is True
    assert cache.get('k') == 'v2'
    assert len(cache) == 1


def test_delete_returns_whether_key_existed(cache: Any) -> None:
    cache.set('k', 'v')
    assert cache.delete('k') is True
    assert cache.delete('k') is False
    assert cache.get('k') is None


def test_delete_missing_returns_false(cache: Any) -> None:
    assert cache.delete('never-set') is False


# ---------------------------------------------------------------------------
# Mapping protocol
# ---------------------------------------------------------------------------


def test_getitem_missing_raises_key_error(cache: Any) -> None:
    with pytest.raises(KeyError):
        cache['missing']


def test_setitem_then_getitem(cache: Any) -> None:
    cache['k'] = 'v'
    assert cache['k'] == 'v'


def test_contains(cache: Any) -> None:
    cache['k'] = 'v'
    assert 'k' in cache
    assert 'other' not in cache


def test_delitem_removes_key(cache: Any) -> None:
    cache['k'] = 'v'
    del cache['k']
    assert 'k' not in cache


def test_delitem_missing_raises_key_error(cache: Any) -> None:
    with pytest.raises(KeyError):
        del cache['missing']


# ---------------------------------------------------------------------------
# pop / touch
# ---------------------------------------------------------------------------


def test_pop_returns_value_and_removes(cache: Any) -> None:
    cache.set('k', 9)
    assert cache.pop('k') == 9
    assert 'k' not in cache


def test_pop_missing_returns_none_by_default(cache: Any) -> None:
    # Unlike dict.pop, a missing key does not raise.
    assert cache.pop('missing') is None


def test_pop_missing_returns_given_default(cache: Any) -> None:
    assert cache.pop('missing', 'd') == 'd'


def test_touch_present_returns_true(cache: Any) -> None:
    cache.set('k', 'v')
    assert cache.touch('k', 10) is True


def test_touch_missing_returns_false_and_creates_nothing(cache: Any) -> None:
    assert cache.touch('missing', 10) is False
    assert 'missing' not in cache
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_context_manager_returns_the_cache(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    cache = make_cache(cache_dir)
    with cache as entered:
        assert entered is cache
        entered.set('k', 'v')


def test_operations_after_close_reopen_lazily(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    cache = make_cache(cache_dir)
    cache.set('k', 'v')
    cache.close()
    # diskcache doesn't refuse use after close(); it reconnects on the next operation.
    assert cache.get('k') == 'v'
    assert cache.set('k2', 'v2') is True
    assert cache.get('k2') == 'v2'


def test_operations_after_context_exit_reopen_lazily(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    cache = make_cache(cache_dir)
    with cache:
        cache.set('k', 'v')
    assert cache.get('k') == 'v'


def test_close_is_idempotent(cache: Any) -> None:
    cache.close()
    cache.close()


def test_data_survives_reopen(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    first = make_cache(cache_dir)
    first.set('k', 'v')
    first.close()
    assert make_cache(cache_dir).get('k') == 'v'


@pytest.mark.diverges(
    backend='oxide',
    ref='D-009',
    reason='oxide Cache is always truthy',
    raises=AssertionError,
)
def test_bool_is_false_when_empty(cache: Any) -> None:
    # Truthiness comes from __len__, so an open, usable cache is falsy while empty.
    assert not cache
    cache.set('k', 'v')
    assert cache


@pytest.mark.backend_only('oxide')
def test_oxide_cache_is_always_truthy(cache: Any) -> None:
    assert cache
    assert cache.enabled is True
    cache.set('k', 'v')
    assert cache


def test_two_caches_on_one_directory_see_each_others_writes(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    a = make_cache(cache_dir)
    b = make_cache(cache_dir)
    a.set('k', 1)
    assert b.get('k') == 1
    b.set('k', 2)
    assert a.get('k') == 2
    b.delete('k')
    assert a.get('k') is None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_default_size_limit(cache: Any) -> None:
    assert cache.size_limit == 2**30


@pytest.mark.requires('cull_limit')
def test_default_cull_limit(cache: Any) -> None:
    assert cache.cull_limit == 10


def test_size_limit_persists_but_timeout_does_not(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    first = make_cache(cache_dir, size_limit=12_345_678, timeout=5)
    assert (first.size_limit, first.timeout) == (12_345_678, 5)
    first.close()

    # The adapter always passes timeout, so open with the default to see what comes back.
    reopened = make_cache(cache_dir)
    assert reopened.size_limit == 12_345_678
    assert reopened.timeout == 60


@pytest.mark.requires('cull_limit')
def test_cull_limit_persists(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    first = make_cache(cache_dir, cull_limit=3)
    assert first.cull_limit == 3
    first.close()

    reopened = make_cache(cache_dir)
    assert reopened.cull_limit == 3


# ---------------------------------------------------------------------------
# Keys (D-004)
# ---------------------------------------------------------------------------


@pytest.mark.diverges(backend='oxide', ref='D-004', reason='oxide accepts str keys only', raises=TypeError)
@pytest.mark.parametrize('key', NON_STR_KEYS)
def test_non_str_keys_round_trip(cache: Any, key: Any) -> None:
    assert cache.set(key, 'v') is True
    assert cache.get(key) == 'v'
    assert key in cache


@pytest.mark.backend_only('oxide')
@pytest.mark.parametrize('key', NON_STR_KEYS)
def test_non_str_keys_raise_type_error(cache: Any, key: Any) -> None:
    with pytest.raises(TypeError):
        cache.set(key, 'v')
    with pytest.raises(TypeError):
        cache.get(key)
    assert len(cache) == 0


# ---------------------------------------------------------------------------
# Timeout exception (D-007)
# ---------------------------------------------------------------------------


def test_timeout_is_an_exception(backend: Backend) -> None:
    assert issubclass(backend.timeout_error, Exception)


@pytest.mark.diverges(
    backend='oxide',
    ref='D-007',
    reason='oxide Timeout also subclasses TimeoutError',
    raises=AssertionError,
)
def test_timeout_bases_are_only_exception(backend: Backend) -> None:
    assert backend.timeout_error.__bases__ == (Exception,)
    assert not issubclass(backend.timeout_error, TimeoutError)


@pytest.mark.backend_only('oxide')
def test_timeout_is_a_timeout_error(backend: Backend) -> None:
    assert issubclass(backend.timeout_error, TimeoutError)


@pytest.mark.diverges(backend='oxide', ref='D-014', reason='oxide rejects bool timeout', raises=TypeError)
def test_timeout_true_is_accepted(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    cache = make_cache(cache_dir, timeout=True)
    assert cache.set('k', 'v') is True


@pytest.mark.backend_only('oxide')
def test_oxide_rejects_bool_timeout(make_cache: Callable[..., Any], cache_dir: Path) -> None:
    with pytest.raises(TypeError):
        make_cache(cache_dir, timeout=True)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def _umask() -> int:
    mask = os.umask(0)
    os.umask(mask)
    return mask


@pytest.mark.diverges(
    backend='oxide',
    ref='D-015',
    reason='oxide creates the cache directory owner-only',
    raises=AssertionError,
)
def test_new_directory_follows_the_umask(make_cache: Callable[..., Any], tmp_path: Path) -> None:
    directory = tmp_path / 'parent' / 'cache'
    make_cache(directory).set('k', 'v')
    assert _mode(directory) == 0o777 & ~_umask()


@pytest.mark.backend_only('oxide')
def test_oxide_new_directory_is_owner_only(make_cache: Callable[..., Any], tmp_path: Path) -> None:
    directory = tmp_path / 'parent' / 'cache'
    make_cache(directory).set('big', b'x' * 1_000_000)
    assert _mode(directory) == 0o700
    # Intermediate parents follow the umask, like os.makedirs.
    assert _mode(directory.parent) == 0o777 & ~_umask()


@pytest.mark.backend_only('oxide')
def test_oxide_existing_directory_permissions_are_left_alone(make_cache: Callable[..., Any], tmp_path: Path) -> None:
    directory = tmp_path / 'shared'
    directory.mkdir(mode=0o770)
    os.chmod(directory, 0o770)
    make_cache(directory).set('k', 'v')
    assert _mode(directory) == 0o770


# ---------------------------------------------------------------------------
# Opening and reading never modify the database (Test E, D-019)
# ---------------------------------------------------------------------------

READ_ONLY_ENTRIES = 50


def _main_db_snapshot(db: Path) -> tuple[str, int, int]:
    # The main database file only: the -shm file changes on any read and is never compared.
    st = os.stat(db)
    return hashlib.sha256(db.read_bytes()).hexdigest(), st.st_size, st.st_mtime_ns


@pytest.mark.diverges(
    backend='diskcache',
    ref='D-019',
    reason='diskcache rewrites its Settings on open',
    raises=AssertionError,
)
def test_open_and_read_never_write(make_cache: Callable[..., Any], cache_dir: Path, backend: Backend) -> None:
    # Fixture: small values with no expiry, so no read has a legitimate reason to write.
    with make_cache(cache_dir) as writer:
        for i in range(READ_ONLY_ENTRIES):
            writer.set(f'k{i}', f'v{i}')
    db = backend.db_path(cache_dir)
    wal = db.with_name(db.name + '-wal')
    if wal.exists() and wal.stat().st_size:
        checkpoint(db)
    assert not wal.exists() or wal.stat().st_size == 0, 'the WAL still holds frames before the snapshot'

    before = _main_db_snapshot(db)
    with SqlitePeer(db) as peer:
        version = peer.data_version()
        # Make an mtime change visible even on a coarse-grained filesystem clock.
        time.sleep(0.01)
        with make_cache(cache_dir) as reader:
            assert reader.get('k1') == 'v1'
            assert 'k2' in reader
            assert len(reader) == READ_ONLY_ENTRIES
            assert len(list(reader.iterkeys())) == READ_ONLY_ENTRIES
            reader.volume()
        assert peer.data_version() == version, 'opening or reading wrote to the database'
    # Checked after the peer closes too, so anything left in the WAL has been folded back in.
    assert _main_db_snapshot(db) == before, 'the main database file changed'
