"""`Cache(on_decode_error=...)`: self-healing reads (plan 08, step 2b).

Under `'miss'` (or a callable), a value no build can read is a miss and its row is deleted; one
another version or host might read is a miss and stays; anything that isn't about the stored bytes
still raises.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import diskcache_oxide
from diskcache_oxide import Cache, CorruptValueError, ForeignValueError, NullCache, Timeout, UnsafeValueError

LOGGER = 'diskcache_oxide'
MODE_MSGPACK = 3


class Movable:
    """A pickled class that the tests "move" by deleting it from this module."""

    def __init__(self, x: int = 1) -> None:
        self.x = x


class Raises:
    """Unpickling raises whatever `error` is set to, from `__setstate__`."""

    error: BaseException = ValueError('broken __setstate__')
    hook: Any = None

    def __init__(self) -> None:
        self.state = 1

    def __getstate__(self) -> dict[str, int]:
        return {'state': 1}

    def __setstate__(self, state: dict[str, int]) -> None:
        if Raises.hook is not None:
            Raises.hook()
        raise Raises.error


@pytest.fixture(autouse=True)
def _reset_raises() -> Iterator[None]:
    yield
    Raises.error = ValueError('broken __setstate__')
    Raises.hook = None


def _sql(directory: Path, statement: str) -> None:
    """Runs one statement on the cache's database from another process, with the stdlib's SQLite."""
    script = 'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\ncon.execute(sys.argv[2])\ncon.commit()\ncon.close()'
    subprocess.run([sys.executable, '-c', script, str(directory / 'diskcache-oxide.sqlite'), statement], check=True)


def _stored(directory: Path, key: str) -> bool:
    """Whether `key` still has a row, checked by a fresh handle that never decodes."""
    with Cache(directory) as other:
        return key in other


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    return tmp_path / 'c'


def _moved_away(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stores a pickled `Movable` under 'k', then removes the class, as a refactor would."""
    with Cache(directory, pickle=True) as writer:
        writer.set('k', Movable())
    monkeypatch.delattr(sys.modules[__name__], 'Movable')


# -- 'raise' keeps today's behavior --------------------------------------------------------------


def test_raise_is_the_default(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _moved_away(directory, monkeypatch)
    cache = Cache(directory, pickle=True)
    with pytest.raises(AttributeError):
        cache.get('k')
    assert _stored(directory, 'k')
    cache.close()


# -- unreadable: a miss, and the row goes ---------------------------------------------------------


def test_a_moved_class_is_a_miss_and_its_row_is_deleted(
    directory: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _moved_away(directory, monkeypatch)
    cache = Cache(directory, pickle=True, on_decode_error='miss')
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert cache.get('k', 'default') == 'default'
    assert not _stored(directory, 'k')
    [record] = [r for r in caplog.records if r.name == LOGGER]
    assert record.levelno == logging.WARNING
    assert "'k'" in record.getMessage() and 'deleted' in record.getMessage()
    assert 'AttributeError' in record.getMessage()
    cache.close()


def test_malformed_bytes_are_a_miss_and_the_row_is_deleted(directory: Path) -> None:
    cache = Cache(directory, on_decode_error='miss')
    cache.set('k', {'a': 1})
    _sql(directory, "UPDATE cache SET value = X'c1' WHERE key = 'k'")  # 0xc1 is never valid msgpack
    assert cache.get('k') is None
    assert not _stored(directory, 'k')
    cache.close()


def test_every_read_is_covered(directory: Path) -> None:
    cache = Cache(directory, on_decode_error='miss')
    for key in ('a', 'b', 'c'):
        cache.set(key, {'v': 1})
        _sql(directory, f"UPDATE cache SET value = X'c1' WHERE key = '{key}'")
    assert cache.get('a', expire_time=True) == (None, None)
    with pytest.raises(KeyError):
        cache['b']
    assert cache.pop('c', 'default') == 'default'
    assert len(cache) == 0
    cache.close()


# -- foreign: a miss, and the row stays -----------------------------------------------------------


def test_an_unknown_mode_is_foreign_and_kept(directory: Path) -> None:
    cache = Cache(directory)
    cache.set('k', {'a': 1})
    _sql(directory, "UPDATE cache SET mode = 99 WHERE key = 'k'")
    with pytest.raises(ForeignValueError):
        cache.get('k')
    assert issubclass(ForeignValueError, CorruptValueError)

    healing = Cache(directory, on_decode_error='miss')
    assert healing.get('k') is None
    assert healing.pop('k', 'default') == 'default'
    assert _stored(directory, 'k'), 'a newer version may have written it'
    with pytest.raises(ForeignValueError):
        cache.pop('k')
    assert _stored(directory, 'k')
    cache.close()
    healing.close()


def test_an_unknown_msgpack_tag_is_foreign_and_kept(directory: Path) -> None:
    cache = Cache(directory, on_decode_error='miss')
    cache.set('k', {'a': 1})
    # {"$nope": 1}: a one-entry map whose key is an unknown tag.
    _sql(directory, f"UPDATE cache SET value = X'81a5{b'$nope'.hex()}01' WHERE key = 'k'")
    assert cache.get('k') is None
    assert _stored(directory, 'k')
    with pytest.raises(ForeignValueError):
        Cache(directory).get('k')
    # pop decodes after its delete, so it can't keep the row; it was asked to remove it anyway.
    assert cache.pop('k', 'default') == 'default'
    assert not _stored(directory, 'k')
    cache.close()


def test_an_unknown_json_tag_is_foreign_and_kept(directory: Path) -> None:
    cache = Cache(directory, serializer='json', on_decode_error='miss')
    cache.set('k', {'a': 1})
    _sql(directory, """UPDATE cache SET value = CAST('{"$nope": 1}' AS BLOB) WHERE key = 'k'""")
    assert cache.get('k') is None
    assert _stored(directory, 'k')
    cache.close()


# -- passthrough: raised, row kept ----------------------------------------------------------------


def test_unsafe_value_error_still_raises(directory: Path) -> None:
    with Cache(directory, pickle=True) as writer:
        writer.set('k', Raises())
    cache = Cache(directory, on_decode_error='miss')
    with pytest.raises(UnsafeValueError):
        cache.get('k')
    with pytest.raises(UnsafeValueError):
        cache.pop('k')
    assert _stored(directory, 'k')
    cache.close()


@pytest.mark.parametrize('error', [Timeout('nested'), MemoryError(), RecursionError(), OSError('disk'), KeyboardInterrupt()])
def test_errors_that_arent_about_the_bytes_still_raise(directory: Path, error: BaseException) -> None:
    with Cache(directory, pickle=True) as writer:
        writer.set('k', Raises())
    Raises.error = error
    cache = Cache(directory, pickle=True, on_decode_error='miss')
    with pytest.raises(type(error)):
        cache.get('k')
    assert _stored(directory, 'k')
    cache.close()


def test_a_refused_spilled_pickle_is_never_read(directory: Path) -> None:
    with Cache(directory, pickle=True) as writer:
        writer.set('big', [Raises.__new__(Raises), b'x' * 200_000])
    value_files = [p for p in directory.rglob('*.val')]
    assert value_files, 'the value spilled to a file'
    for p in value_files:
        p.write_bytes(b'')  # If the reader opened it, it would see a short file and miss.
    with pytest.raises(UnsafeValueError):
        Cache(directory).get('big')


# -- deleting exactly what failed, without waiting ------------------------------------------------


def test_only_the_version_that_failed_is_deleted(directory: Path) -> None:
    with Cache(directory, pickle=True) as writer:
        writer.set('k', Raises())
    peer = Cache(directory)
    Raises.hook = lambda: peer.set('k', 'fresh')
    cache = Cache(directory, pickle=True, on_decode_error='miss')
    assert cache.get('k') is None
    Raises.hook = None
    assert cache.get('k') == 'fresh', "a peer's newer value must survive"
    cache.close()
    peer.close()


def test_a_busy_write_lock_in_another_process_never_delays_the_miss(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.conformance.proc_workers import SqlitePeer

    _moved_away(directory, monkeypatch)
    cache = Cache(directory, pickle=True, timeout=30, on_decode_error='miss')
    with SqlitePeer(directory / 'diskcache-oxide.sqlite') as peer:
        peer.begin('IMMEDIATE')
        started = time.monotonic()
        assert cache.get('k') is None
        assert time.monotonic() - started < 2
        peer.rollback()
    assert _stored(directory, 'k'), 'the delete was skipped, not waited for'
    assert cache.get('k') is None
    assert not _stored(directory, 'k'), 'and a later read retried it'
    cache.close()


def test_a_write_transaction_on_another_thread_never_delays_the_miss(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _moved_away(directory, monkeypatch)
    cache = Cache(directory, pickle=True, timeout=30, on_decode_error='miss')
    other = Cache(directory)
    holding, done = threading.Event(), threading.Event()

    def hold() -> None:
        with other._hold_write_transaction():
            holding.set()
            done.wait(10)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert holding.wait(10)
        started = time.monotonic()
        assert cache.get('k') is None
        assert time.monotonic() - started < 2
    finally:
        done.set()
        holder.join()
    assert _stored(directory, 'k')
    other.close()
    cache.close()


def test_inside_this_threads_own_transaction_the_delete_is_skipped(directory: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _moved_away(directory, monkeypatch)
    cache = Cache(directory, pickle=True, on_decode_error='miss')
    with cache._hold_write_transaction():
        assert cache.get('k') is None
    assert _stored(directory, 'k')
    cache.close()


# -- the callable ---------------------------------------------------------------------------------


def test_a_callable_gets_the_key_and_the_original_error_after_the_delete(
    directory: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _moved_away(directory, monkeypatch)
    calls: list[tuple[str, BaseException, bool]] = []
    cache: Cache

    def report(key: str, exc: Exception) -> None:
        calls.append((key, exc, key in cache))

    cache = Cache(directory, pickle=True, on_decode_error=report)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert cache.get('k', 'default') == 'default'
    [(key, exc, still_there)] = calls
    assert key == 'k'
    assert isinstance(exc, AttributeError)
    assert not still_there, 'called after the row was deleted'
    assert not [r for r in caplog.records if r.name == LOGGER], 'the callable replaces the warning'
    cache.close()


def test_a_failing_callable_is_logged_and_the_read_is_still_a_miss(
    directory: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _moved_away(directory, monkeypatch)

    def report(key: str, exc: Exception) -> None:
        raise RuntimeError('sentry is down')

    cache = Cache(directory, pickle=True, on_decode_error=report)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert cache.get('k') is None
    [record] = [r for r in caplog.records if r.name == LOGGER]
    assert record.levelno == logging.ERROR
    assert record.exc_info is not None and isinstance(record.exc_info[1], RuntimeError)
    cache.close()


def test_a_callable_that_reads_the_bad_key_again_is_not_called_again(
    directory: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _moved_away(directory, monkeypatch)
    calls: list[str] = []
    cache: Cache

    def report(key: str, exc: Exception) -> None:
        calls.append(key)
        assert cache.get(key) is None  # The delete was skipped, so this fails to decode again.

    cache = Cache(directory, pickle=True, on_decode_error=report)
    with caplog.at_level(logging.WARNING, logger=LOGGER), cache._hold_write_transaction():
        assert cache.get('k') is None
    assert calls == ['k']
    assert len([r for r in caplog.records if r.name == LOGGER]) == 1, 'the nested failure was logged instead'
    cache.close()


# -- validation -----------------------------------------------------------------------------------


@pytest.mark.parametrize('make', [Cache, NullCache], ids=['Cache', 'NullCache'])
def test_on_decode_error_is_validated(tmp_path: Path, make: Any) -> None:
    with pytest.raises(ValueError):
        make(tmp_path / 'c', on_decode_error='ignore')
    with pytest.raises(TypeError):
        make(tmp_path / 'c', on_decode_error=5)
    for policy in ('raise', 'miss', lambda key, exc: None):
        make(tmp_path / 'c', on_decode_error=policy).close()


def test_foreign_value_error_is_public_and_pickles() -> None:
    import pickle

    assert diskcache_oxide.ForeignValueError is ForeignValueError
    assert ForeignValueError.__module__ == 'diskcache_oxide'
    error = pickle.loads(pickle.dumps(ForeignValueError('x')))
    assert type(error) is ForeignValueError
