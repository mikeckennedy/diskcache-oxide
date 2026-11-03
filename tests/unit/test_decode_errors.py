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

import diskcache_oxide
import pytest
from diskcache_oxide import (
    Cache,
    CorruptValueError,
    ForeignValueError,
    NullCache,
    Timeout,
    UnsafeValueError,
)

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
    script = (
        'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\ncon.execute(sys.argv[2])\ncon.commit()\ncon.close()'
    )
    subprocess.run(
        [
            sys.executable,
            '-c',
            script,
            str(directory / 'diskcache-oxide.sqlite'),
            statement,
        ],
        check=True,
    )


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
    # pop decodes before it deletes, so a newer peer's value survives it.
    assert cache.pop('k', 'default') == 'default'
    assert _stored(directory, 'k')
    with pytest.raises(ForeignValueError):
        Cache(directory).pop('k')
    assert _stored(directory, 'k')
    cache.close()


def test_an_unknown_json_tag_is_foreign_and_kept(directory: Path) -> None:
    cache = Cache(directory, serializer='json', on_decode_error='miss')
    cache.set('k', {'a': 1})
    _sql(
        directory,
        """UPDATE cache SET value = CAST('{"$nope": 1}' AS BLOB) WHERE key = 'k'""",
    )
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


@pytest.mark.parametrize(
    'error',
    [
        Timeout('nested'),
        MemoryError(),
        RecursionError(),
        OSError('disk'),
        KeyboardInterrupt(),
    ],
)
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


def test_a_busy_write_lock_in_another_process_never_delays_the_miss(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_a_write_transaction_on_another_thread_never_delays_the_miss(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


def test_inside_this_threads_own_transaction_the_delete_is_skipped(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    with (
        caplog.at_level(logging.WARNING, logger=LOGGER),
        cache._hold_write_transaction(),
    ):
        assert cache.get('k') is None
    assert calls == ['k']
    assert not [r for r in caplog.records if r.name == LOGGER], 'a failure inside a report is not reported again'
    cache.close()


def test_a_logging_handler_that_reads_the_bad_key_cant_recurse(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _moved_away(directory, monkeypatch)
    cache = Cache(directory, pickle=True, on_decode_error='miss')
    seen: list[str] = []

    class ReadsTheCache(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())
            cache.get('k')

    handler = ReadsTheCache()
    logger = logging.getLogger(LOGGER)
    logger.addHandler(handler)
    try:
        with cache._hold_write_transaction():  # So the bad row stays and fails each time.
            assert cache.get('k') is None
    finally:
        logger.removeHandler(handler)
    assert len(seen) == 1
    cache.close()


def test_the_warning_carries_the_traceback(
    directory: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _moved_away(directory, monkeypatch)
    cache = Cache(directory, pickle=True, on_decode_error='miss')
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        cache.get('k')
    [record] = [r for r in caplog.records if r.name == LOGGER]
    assert record.exc_info is not None and isinstance(record.exc_info[1], AttributeError)
    cache.close()


def test_a_row_that_stays_is_reported_once(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    cache = Cache(directory, on_decode_error='miss')
    cache.set('k', {'a': 1})
    _sql(directory, "UPDATE cache SET mode = 99 WHERE key = 'k'")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(5):
            assert cache.get('k') is None
    assert len([r for r in caplog.records if r.name == LOGGER]) == 1
    cache.set('k', {'a': 2})
    _sql(directory, "UPDATE cache SET mode = 99 WHERE key = 'k'")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert cache.get('k') is None
    assert len([r for r in caplog.records if r.name == LOGGER]) == 2, 'a new version is reported again'
    cache.close()


def test_a_callable_covers_pop_too(directory: Path) -> None:
    calls: list[str] = []
    cache = Cache(directory, on_decode_error=lambda key, exc: calls.append(key))
    cache.set('k', {'a': 1})
    _sql(directory, "UPDATE cache SET value = X'c1' WHERE key = 'k'")
    assert cache.pop('k', 'default') == 'default'
    assert calls == ['k']
    assert not _stored(directory, 'k')
    cache.close()


def test_pop_of_a_foreign_tag_says_it_was_kept(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    cache = Cache(directory, on_decode_error='miss')
    cache.set('k', {'a': 1})
    _sql(directory, f"UPDATE cache SET value = X'81a5{b'$nope'.hex()}01' WHERE key = 'k'")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert cache.pop('k') is None
        assert cache.pop('k') is None
        assert cache.get('k') is None
    [record] = [r for r in caplog.records if r.name == LOGGER]
    assert 'left in place' in record.getMessage()
    assert 'ForeignValueError' in record.getMessage()
    assert _stored(directory, 'k')
    cache.close()


# -- foreign values from this host's limits -------------------------------------------------------


def test_a_time_zone_this_host_lacks_is_foreign(directory: Path) -> None:
    import datetime as dt
    import zoneinfo

    cache = Cache(directory, serializer='json', on_decode_error='miss')
    cache.set(
        'k',
        dt.datetime(2026, 10, 30, 12, tzinfo=zoneinfo.ZoneInfo('America/Los_Angeles')),
    )
    _sql(
        directory,
        "UPDATE cache SET value = CAST(replace(CAST(value AS TEXT), 'America/Los_Angeles', 'Mars/Olympus') AS BLOB) WHERE key = 'k'",
    )
    assert cache.get('k') is None
    assert _stored(directory, 'k')
    with pytest.raises(ForeignValueError) as info:
        Cache(directory).get('k')
    assert isinstance(info.value.__cause__, zoneinfo.ZoneInfoNotFoundError)
    assert cache.pop('k', 'default') == 'default'
    assert _stored(directory, 'k'), 'a host with that time zone can read it'
    with pytest.raises(ForeignValueError):
        Cache(directory).pop('k')
    assert _stored(directory, 'k')
    cache.close()


def test_an_integer_over_this_hosts_digit_limit_is_foreign(directory: Path) -> None:
    before = sys.get_int_max_str_digits()
    cache = Cache(directory, on_decode_error='miss')
    try:
        sys.set_int_max_str_digits(0)
        cache.set('k', 10**5000)
        assert cache.get('k') == 10**5000
        sys.set_int_max_str_digits(4300)
        assert cache.get('k') is None
        assert _stored(directory, 'k'), 'another host (or this one, with a higher limit) can read it'
    finally:
        sys.set_int_max_str_digits(before)
    cache.close()


def test_an_unknown_json_tag_raises_foreign_under_raise(directory: Path) -> None:
    cache = Cache(directory, serializer='json')
    cache.set('k', [1, {'a': 1}])
    _sql(
        directory,
        """UPDATE cache SET value = CAST('[1, {"$nope": 1}]' AS BLOB) WHERE key = 'k'""",
    )
    with pytest.raises(ForeignValueError):
        cache.get('k')
    cache.close()


# -- errors from a cache call inside a pickle hook ------------------------------------------------


@pytest.mark.parametrize(('nested', 'kept'), [('corrupt', False), ('foreign', True)])
def test_a_decode_error_from_a_nested_read_classifies_the_outer_value(directory: Path, nested: str, kept: bool) -> None:
    # A hook that fails because of another key's bad value: a corrupt one counts as "the hook
    # failed" (the outer row goes, as DurableCache would do), a foreign one keeps the outer row.
    with Cache(directory, pickle=True) as writer:
        writer.set('outer', Raises())
        writer.set('inner', {'a': 1})
    mode = "value = X'c1'" if nested == 'corrupt' else 'mode = 99'
    _sql(directory, f"UPDATE cache SET {mode} WHERE key = 'inner'")
    strict = Cache(directory)

    def read_inner() -> None:
        strict.get('inner')

    Raises.hook = read_inner
    cache = Cache(directory, pickle=True, on_decode_error='miss')
    assert cache.get('outer') is None
    Raises.hook = None
    assert _stored(directory, 'outer') is kept
    cache.close()
    strict.close()


# -- garbage collection ---------------------------------------------------------------------------


def test_a_callable_that_refers_back_to_its_cache_is_collected(directory: Path) -> None:
    import gc
    import weakref

    class Reporter:
        cache: Any = None

        def __call__(self, key: str, exc: Exception) -> None:
            pass

    reporter = Reporter()
    cache = Cache(directory, on_decode_error=reporter)
    reporter.cache = cache
    gone = weakref.ref(reporter)
    del reporter, cache
    gc.collect()
    assert gone() is None, 'the cache and its callable formed a cycle the collector could not see'


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
