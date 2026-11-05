"""`Cache(durable=True)` answers an outage on a single-key call as a miss (plan 09, step 2; D-028).

Failures are forced with what really makes each call fail. A peer process holding SQLite's write lock
fails a write, but not a read: WAL readers don't wait for it. So reads fail through `max_connections=1`
with another thread inside a hook, a value file with no permissions, or a damaged database, and through
a stuck fork in `tests/unit/durable_scenarios.py`, which also has the fork and `RLIMIT_FSIZE` cases.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Generator, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import (
    Cache,
    Caches,
    CorruptValueError,
    NullCache,
    Timeout,
    UnsafeValueError,
    _core,
)

from tests.conformance.proc_workers import SqlitePeer
from tests.unit.durable_scenarios import SCENARIOS

REPO_ROOT = Path(__file__).resolve().parents[2]
LOGGER = 'diskcache_oxide'
BIG = 70_000  # over the 64 KiB spill threshold
SHORT = 0.05
WATCHDOG = 20.0
FOREIGN_TAG = f"X'81a5{b'$nope'.hex()}01'"  # {"$nope": 1}: a known mode with an unknown tag.
NOT_AS_ROOT = pytest.mark.skipif(hasattr(os, 'geteuid') and os.geteuid() == 0, reason='root ignores file permissions')


class Hooked:
    """Unpickling it calls `Hooked.hook`, if one is set."""

    hook: Callable[[], None] | None = None

    def __init__(self, payload: str = 'x') -> None:
        self.payload = payload

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        if Hooked.hook is not None:
            Hooked.hook()


class RaisesOnPickle:
    """Pickling it raises `RaisesOnPickle.error`: an exception from the caller's code, during a `set`."""

    error: BaseException = Timeout('from a __reduce__')

    def __reduce__(self) -> tuple[Any, ...]:
        raise RaisesOnPickle.error


@pytest.fixture(autouse=True)
def _reset_hook() -> Iterator[None]:
    yield
    Hooked.hook = None


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    return tmp_path / 'c'


def _db(directory: Path) -> Path:
    return directory / 'diskcache-oxide.sqlite'


def _sql(directory: Path, statement: str) -> None:
    """Runs one statement on the cache's database from another process, with the stdlib's SQLite."""
    script = (
        'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\ncon.execute(sys.argv[2])\ncon.commit()\ncon.close()'
    )
    subprocess.run([sys.executable, '-c', script, str(_db(directory)), statement], check=True)


def _value_file(directory: Path) -> Path:
    [path] = list(directory.rglob('*.val'))
    return path


@contextmanager
def peer_holding_the_write_lock(directory: Path) -> Generator[None]:
    with SqlitePeer(_db(directory)) as peer:
        peer.begin('IMMEDIATE')
        try:
            yield
        finally:
            peer.rollback()


@contextmanager
def unreadable(path: Path) -> Generator[None]:
    os.chmod(path, 0)
    try:
        yield
    finally:
        os.chmod(path, 0o600)


@contextmanager
def unwritable_directory(path: Path) -> Generator[None]:
    """A value file can't be created under `path`. Its database files are already open."""
    mode = path.stat().st_mode
    for each in (path, *[p for p in path.rglob('*') if p.is_dir()]):
        os.chmod(each, 0o500)
    try:
        yield
    finally:
        for each in (path, *[p for p in path.rglob('*') if p.is_dir()]):
            os.chmod(each, mode)


def _messages(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == LOGGER]


def _total(stats: dict[str, dict[str, int]], bucket: str) -> int:
    return sum(stats[bucket].values())


# -- the table: writes behind a peer holding the write lock -----------------------------------------


def test_every_write_is_answered_as_the_table_says(directory: Path) -> None:
    cache = Cache(directory, timeout=SHORT, durable=True)
    cache.set('k', 'old')
    cache.set('n', 10)
    with peer_holding_the_write_lock(directory):
        assert cache.set('k', 'new') is False
        cache['k'] = 'new'  # Dropped, without raising.
        assert cache.set('spilled', b'x' * BIG) is False
        assert cache.add('added', 1) is False
        assert cache.touch('k', 60) is False
        assert cache.delete('k') is False
        with pytest.raises(KeyError):
            del cache['k']
        assert cache.pop('k', 'default') == 'default'
        assert cache.pop('k') is None
        assert cache.incr('n') == 1, 'default + delta, from the default 0'
        assert cache.incr('n', 2, 5) == 7
        assert cache.incr('n', 0.5, 5) == 5.5
        assert cache.decr('n') == -1
        assert cache.decr('n', 2, 5) == 3
        with pytest.raises(KeyError):
            cache.incr('n', 1, None)
        with pytest.raises(KeyError):
            cache.decr('n', 1, None)
        # A peer holding the write lock doesn't stop a read.
        assert cache.get('k') == 'old'
        assert 'k' in cache
    # Every write was dropped, and every failed removal left its row.
    assert cache.get('k') == 'old'
    assert cache.get('n') == 10
    assert cache.get('added') is None
    assert cache.get('spilled') is None
    assert not list(directory.rglob('*.val')), 'a dropped set left its value file'
    assert cache.get('k', expire_time=True) == ('old', None), 'the dropped touch changed nothing'
    # And it all works again.
    assert cache.set('k', 'new') is True
    assert cache.delete('k') is True
    assert cache.incr('n') == 11
    cache.close()


def test_the_default_still_raises(directory: Path) -> None:
    cache = Cache(directory, timeout=SHORT)
    cache.set('k', 'old')
    cache.set('n', 10)
    calls: dict[str, Callable[[], Any]] = {
        'set': lambda: cache.set('k', 'new'),
        'add': lambda: cache.add('added', 1),
        'touch': lambda: cache.touch('k', 60),
        'delete': lambda: cache.delete('k'),
        'pop': lambda: cache.pop('k'),
        'incr': lambda: cache.incr('n'),
        'decr': lambda: cache.decr('n'),
    }
    with peer_holding_the_write_lock(directory):
        for name, call in calls.items():
            with pytest.raises(Timeout):
                call()
                pytest.fail(f'{name} returned')
    assert cache._durable_stats() == {}
    cache.close()


def test_durable_false_is_the_default_and_explicit(directory: Path) -> None:
    cache = Cache(directory, timeout=SHORT, durable=False)
    cache.set('k', 1)
    with peer_holding_the_write_lock(directory), pytest.raises(Timeout):
        cache.set('k', 2)
    cache.close()


def test_a_success_answers_as_usual(directory: Path) -> None:
    cache = Cache(directory, durable=True)
    assert cache.set('k', 'v') is True
    assert cache.get('k') == 'v'
    assert cache['k'] == 'v'
    assert 'k' in cache
    assert cache.add('k', 'other') is False
    assert cache.touch('k', 60) is True
    assert cache.incr('n', 2, 5) == 7
    assert cache.pop('k') == 'v'
    assert cache.get('k', 'default') == 'default'
    assert cache.delete('k') is False
    with pytest.raises(KeyError):
        cache['k']
    assert _total(cache._durable_stats(), 'busy') == 0
    assert _total(cache._durable_stats(), 'broken') == 0
    cache.close()


# -- the table: reads ---------------------------------------------------------------------------------


def test_a_pool_taken_by_a_slow_hook_fails_reads_as_a_miss(directory: Path) -> None:
    """`max_connections=1` with another thread inside a hook on an inline value, so it keeps the one
    connection. The other thread's read then can't get one."""
    cache = Cache(directory, timeout=SHORT, pickle=True, max_connections=1, durable=True)
    plain = Cache(directory, timeout=SHORT, pickle=True, max_connections=1)
    cache.set('slow', Hooked())
    cache.set('k', 'v')
    entered, release = threading.Event(), threading.Event()

    def hook() -> None:
        entered.set()
        assert release.wait(WATCHDOG)

    Hooked.hook = hook
    reader = threading.Thread(target=lambda: cache.get('slow'), daemon=True)
    reader.start()
    try:
        assert entered.wait(WATCHDOG)
        Hooked.hook = None
        assert cache.get('k', 'default') == 'default'
        assert cache.get('k', 'default', expire_time=True) == ('default', None)
        with pytest.raises(KeyError):
            cache['k']
        assert 'k' not in cache
        assert cache.pop('k', 'default') == 'default'
        # A handle that isn't durable, on the same pool.
        with pytest.raises(Timeout):
            plain.get('k')
        with pytest.raises(Timeout):
            'k' in plain  # noqa: B015
    finally:
        release.set()
        reader.join(WATCHDOG)
    assert not reader.is_alive()
    assert cache.get('k') == 'v', 'the pop that failed left the row'
    busy = cache._durable_stats()['busy']
    assert (busy['get'], busy['in'], busy['pop']) == (3, 1, 1)
    plain.close()
    cache.close()


@NOT_AS_ROOT
def test_a_value_file_it_cant_read_is_a_miss_and_stays(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    cache = Cache(directory, durable=True)
    cache.set('k', b'x' * BIG)
    path = _value_file(directory)
    with unreadable(path), caplog.at_level(logging.WARNING, logger=LOGGER):
        assert cache.get('k', 'default') == 'default'
        with pytest.raises(KeyError):
            cache['k']
        assert cache.pop('k', 'default') == 'default'
    assert cache._durable_stats()['broken']['get'] == 2
    assert cache._durable_stats()['broken']['pop'] == 1
    [record] = _messages(caplog)
    assert record.levelno == logging.ERROR
    assert 'get: 1' in record.getMessage() and 'Permission denied' in record.getMessage()
    assert cache.get('k') == b'x' * BIG, 'neither call removed it'
    cache.close()


@NOT_AS_ROOT
def test_the_default_raises_on_a_value_file_it_cant_read(directory: Path) -> None:
    cache = Cache(directory)
    cache.set('k', b'x' * BIG)
    with unreadable(_value_file(directory)):
        with pytest.raises(PermissionError):
            cache.get('k')
        with pytest.raises(PermissionError):
            cache.pop('k')
    cache.close()


@pytest.mark.parametrize('durable', [True, False])
@pytest.mark.parametrize('size', [2**63 - 1, 2**62, 2**40, -1])
def test_a_size_the_value_file_doesnt_match_is_a_miss(directory: Path, durable: bool, size: int) -> None:
    # Allocating the size up front used to abort the whole process.
    cache = Cache(directory, durable=durable)
    cache.set('k', b'x' * BIG)
    cache.set('p', b'y' * BIG)
    _sql(directory, f'UPDATE cache SET size = {size}')
    assert cache.get('k', 'default') == 'default'
    assert cache.pop('p', 'default') == 'default'
    cache.close()


@pytest.mark.skipif(
    bool(os.environ.get('DCO_EMULATED')),
    reason='under Rosetta (linux-check.sh --platform) a failed allocation kills the process instead of failing',
)
@pytest.mark.parametrize('durable', [True, False])
def test_a_value_too_big_to_allocate_is_a_memory_error_and_stays(directory: Path, durable: bool) -> None:
    size = 2**54  # past any address space, and a sparse file APFS allows
    cache = Cache(directory, durable=durable)
    cache.set('k', b'x' * BIG)
    try:
        os.truncate(_value_file(directory), size)
    except OSError:
        pytest.skip('this filesystem has no sparse file that big')
    _sql(directory, f'UPDATE cache SET size = {size}')
    with pytest.raises(MemoryError):
        cache.get('k')
    with pytest.raises(MemoryError):
        cache.pop('k')
    assert 'k' in cache
    cache.close()


def test_a_damaged_database_is_a_miss(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    cache = Cache(directory, timeout=SHORT, durable=True)
    plain = Cache(directory, timeout=SHORT)
    cache.set('k', 1)
    for path in directory.glob('diskcache-oxide.sqlite*'):
        path.write_bytes(b'\xff' * path.stat().st_size)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert cache.get('k', 'default') == 'default'
        assert 'k' not in cache
        assert cache.set('k', 2) is False
        assert cache.add('a', 2) is False
        assert cache.touch('k') is False
        assert cache.pop('k', 'default') == 'default'
        assert cache.incr('n', 1, 5) == 6
        assert cache.decr('n', 1, 5) == 4
        assert cache.delete('k') is False
    records = _messages(caplog)
    assert {r.levelno for r in records} == {logging.ERROR}
    assert 'not a database' in records[0].getMessage()
    with pytest.raises(OSError, match='not a database'):
        plain.get('k')
    # Admin calls need the truth.
    with pytest.raises(OSError, match='not a database'):
        len(cache)
    with pytest.raises(OSError, match='not a database'):
        cache.clear()
    plain.close()
    cache.close()


@NOT_AS_ROOT
def test_a_value_file_it_cant_create_drops_the_set(directory: Path) -> None:
    cache = Cache(directory, durable=True)
    plain = Cache(directory)
    cache.set('k', 'old')
    with unwritable_directory(directory):
        assert cache.set('k', b'x' * BIG) is False
        with pytest.raises(PermissionError):
            plain.set('k', b'x' * BIG)
    assert cache.get('k') == 'old'
    assert cache._durable_stats()['broken']['set'] == 1
    plain.close()
    cache.close()


# -- pop ------------------------------------------------------------------------------------------------


def test_a_pop_whose_delete_fails_returns_the_default_and_the_value_stays(
    directory: Path,
) -> None:
    """Returning the decoded value here could deliver it twice, once to this caller and once to whoever
    pops it after the failure."""
    cache = Cache(directory, timeout=SHORT, durable=True)
    cache.set('k', 'once')
    with peer_holding_the_write_lock(directory):
        assert cache.pop('k', 'default') == 'default'
    assert cache._durable_stats()['busy']['pop'] == 1
    assert cache.pop('k') == 'once'
    assert cache.pop('k', 'default') == 'default'
    cache.close()


def test_a_failed_delete_of_an_unreadable_value_is_a_miss_and_the_row_stays(
    directory: Path,
) -> None:
    seen: list[str] = []
    cache = Cache(
        directory,
        timeout=SHORT,
        durable=True,
        on_decode_error=lambda key, exc: seen.append(key),
    )
    cache.set('k', {'a': 1})
    _sql(directory, "UPDATE cache SET value = X'c1' WHERE key = 'k'")
    with peer_holding_the_write_lock(directory):
        assert cache.pop('k', 'default') == 'default'
    assert cache._durable_stats()['busy']['pop'] == 1
    assert seen == ['k'], 'the decode failure was reported too'
    assert 'k' in cache
    assert cache.pop('k', 'default') == 'default'
    assert 'k' not in cache, 'the next pop deleted it'
    cache.close()


def test_an_explicit_raise_gets_the_decode_error_when_the_delete_fails(
    directory: Path,
) -> None:
    cache = Cache(directory, timeout=SHORT, durable=True, on_decode_error='raise')
    cache.set('k', {'a': 1})
    _sql(directory, "UPDATE cache SET value = X'c1' WHERE key = 'k'")
    with peer_holding_the_write_lock(directory), pytest.raises(CorruptValueError):
        cache.pop('k')
    assert 'k' in cache
    assert cache._durable_stats()['busy']['pop'] == 1
    cache.close()


def test_a_foreign_value_survives_a_durable_pop(directory: Path) -> None:
    cache = Cache(directory, durable=True)
    cache.set('k', {'a': 1})
    _sql(directory, f"UPDATE cache SET value = {FOREIGN_TAG} WHERE key = 'k'")
    assert cache.pop('k', 'default') == 'default'
    assert 'k' in cache
    cache.close()


def test_a_race_lost_past_the_deadline_is_a_miss(directory: Path) -> None:
    """A hook that overwrites its own key and outlasts the timeout: `pop` runs out of time."""
    cache = Cache(directory, timeout=0.02, pickle=True, durable=True)
    other = Cache(directory, timeout=30.0, pickle=True)
    cache.set('k', Hooked())

    def overwrite() -> None:
        other.set('k', Hooked())
        threading.Event().wait(0.03)

    Hooked.hook = overwrite
    assert cache.pop('k', 'default') == 'default'
    assert cache._durable_stats()['busy']['pop'] == 1
    Hooked.hook = None
    assert 'k' in cache, 'this pop deletes nothing'
    other.close()
    cache.close()


# -- on_decode_error ------------------------------------------------------------------------------------


def _corrupted(directory: Path, **settings: Any) -> Cache:
    cache = Cache(directory, **settings)
    cache.set('k', {'a': 1})
    _sql(directory, "UPDATE cache SET value = X'c1' WHERE key = 'k'")
    return cache


def test_on_decode_error_defaults_to_miss_when_durable(directory: Path) -> None:
    cache = _corrupted(directory, durable=True)
    assert cache.get('k', 'default') == 'default'
    assert 'k' not in cache, 'the unreadable row went, as under an explicit miss'
    cache.close()


def test_on_decode_error_none_is_the_default_either_way(directory: Path, tmp_path: Path) -> None:
    durable = _corrupted(directory, durable=True, on_decode_error=None)
    assert durable.get('k', 'default') == 'default'
    durable.close()
    plain = _corrupted(tmp_path / 'plain', on_decode_error=None)
    with pytest.raises(CorruptValueError):
        plain.get('k')
    plain.close()


def test_an_explicit_on_decode_error_wins_over_durable(directory: Path, tmp_path: Path) -> None:
    raising = _corrupted(directory, durable=True, on_decode_error='raise')
    with pytest.raises(CorruptValueError):
        raising.get('k')
    raising.close()
    seen: list[str] = []
    reporting = _corrupted(
        tmp_path / 'report',
        durable=True,
        on_decode_error=lambda key, exc: seen.append(key),
    )
    assert reporting.get('k') is None
    assert seen == ['k']
    reporting.close()


def test_a_bad_on_decode_error_still_raises(directory: Path) -> None:
    with pytest.raises(ValueError, match='on_decode_error'):
        Cache(directory, durable=True, on_decode_error='ignore')  # ty: ignore[invalid-argument-type]
    with pytest.raises(TypeError, match='on_decode_error'):
        Cache(directory, durable=True, on_decode_error=1)  # ty: ignore[invalid-argument-type]


# -- pickle hooks ---------------------------------------------------------------------------------------

OUTAGES = [
    Timeout('nested'),
    TimeoutError('nested'),
    OSError('disk'),
    ConnectionError('gone'),
    RecursionError(),
]
ALWAYS_RAISED = [
    MemoryError(),
    KeyboardInterrupt(),
    SystemExit(),
    UnsafeValueError('from a hook'),
]


def _raising(error: BaseException) -> Callable[[], None]:
    def fail() -> None:
        raise error

    return fail


@pytest.mark.parametrize('spilled', [False, True], ids=['inline', 'spilled'])
@pytest.mark.parametrize('error', OUTAGES, ids=lambda e: type(e).__name__)
def test_an_outage_from_a_hook_is_a_miss_with_the_row_kept_reported_once(
    directory: Path,
    caplog: pytest.LogCaptureFixture,
    error: BaseException,
    spilled: bool,
) -> None:
    cache = Cache(directory, pickle=True, durable=True)
    cache.set('k', Hooked('x' * (BIG if spilled else 1)))
    Hooked.hook = _raising(error)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert cache.get('k', 'default') == 'default'
        assert cache.get('k', 'default') == 'default'
        assert cache.pop('k', 'default') == 'default'
        with pytest.raises(KeyError):
            cache['k']
    [record] = _messages(caplog)
    assert 'left in place' in record.getMessage()
    assert record.levelno == logging.WARNING
    Hooked.hook = None
    assert cache.get('k').payload[0] == 'x', 'the row stayed'
    assert cache.pop('k').payload[0] == 'x'
    cache.close()


@pytest.mark.parametrize('error', ALWAYS_RAISED, ids=lambda e: type(e).__name__)
def test_what_a_hook_raises_that_is_never_a_miss(directory: Path, error: BaseException) -> None:
    cache = Cache(directory, pickle=True, durable=True)
    cache.set('k', Hooked('kept'))
    Hooked.hook = _raising(error)
    with pytest.raises(type(error)):
        cache.get('k')
    with pytest.raises(type(error)):
        cache.pop('k')
    Hooked.hook = None
    assert cache.pop('k').payload == 'kept'
    cache.close()


def test_a_callable_policy_hears_about_a_hook_outage_once(directory: Path) -> None:
    seen: list[tuple[str, type]] = []
    cache = Cache(
        directory,
        pickle=True,
        durable=True,
        on_decode_error=lambda key, exc: seen.append((key, type(exc))),
    )
    cache.set('k', Hooked('kept'))
    Hooked.hook = _raising(Timeout('nested'))
    assert cache.get('k') is None
    assert cache.get('k') is None
    assert seen == [('k', Timeout)]
    cache.close()


@pytest.mark.parametrize('error', OUTAGES, ids=lambda e: type(e).__name__)
def test_an_explicit_raise_raises_a_hook_outage(directory: Path, error: BaseException) -> None:
    cache = Cache(directory, pickle=True, durable=True, on_decode_error='raise')
    cache.set('k', Hooked('kept'))
    Hooked.hook = _raising(error)
    with pytest.raises(type(error)):
        cache.get('k')
    with pytest.raises(type(error)):
        cache.pop('k')
    Hooked.hook = None
    assert 'k' in cache
    cache.close()


def test_a_hook_outage_raises_without_durable(directory: Path) -> None:
    cache = Cache(directory, pickle=True, on_decode_error='miss')
    cache.set('k', Hooked('kept'))
    Hooked.hook = _raising(Timeout('nested'))
    with pytest.raises(Timeout):
        cache.get('k')
    cache.close()


def test_a_failing_unpickle_is_still_unreadable(directory: Path) -> None:
    cache = Cache(directory, pickle=True, durable=True)
    cache.set('k', Hooked('x'))
    Hooked.hook = _raising(ValueError('broken __setstate__'))
    assert cache.get('k', 'default') == 'default'
    Hooked.hook = None
    assert 'k' not in cache, 'deleted, as under an explicit miss'
    cache.close()


# -- code that isn't this cache's -------------------------------------------------------------------------


def test_a_timeout_from_the_callers_clock_raises(directory: Path) -> None:
    def clock() -> float:
        raise Timeout('from the clock')

    cache = Cache(directory, durable=True, clock=clock)
    calls: list[Callable[[], Any]] = [
        lambda: cache.get('k'),
        lambda: cache['k'],
        lambda: 'k' in cache,
        lambda: cache.set('k', 1),
        lambda: cache.add('k', 1),
        lambda: cache.touch('k'),
        lambda: cache.delete('k'),
        lambda: cache.pop('k'),
        lambda: cache.incr('k'),
        lambda: cache.decr('k'),
    ]
    for call in calls:
        with pytest.raises(Timeout, match='from the clock'):
            call()
    cache.close()


@pytest.mark.parametrize(
    'error',
    [Timeout('from a __reduce__'), OSError('from a __reduce__')],
    ids=lambda e: type(e).__name__,
)
def test_an_error_from_the_callers_pickle_hook_during_a_write_raises(directory: Path, error: BaseException) -> None:
    RaisesOnPickle.error = error
    cache = Cache(directory, pickle=True, durable=True)
    for call in (
        lambda: cache.set('k', RaisesOnPickle()),
        lambda: cache.add('k', RaisesOnPickle()),
    ):
        with pytest.raises(type(error), match='from a __reduce__'):
            call()
    assert _total(cache._durable_stats(), 'busy') == 0
    assert _total(cache._durable_stats(), 'broken') == 0
    cache.close()


# -- bugs still raise -------------------------------------------------------------------------------------


def test_bugs_still_raise(directory: Path) -> None:
    cache = Cache(directory, durable=True)
    cache.set('text', 'not a number')
    calls: list[tuple[type[BaseException], Callable[[], Any]]] = [
        (TypeError, lambda: cache.get(1)),  # ty: ignore[invalid-argument-type]
        (TypeError, lambda: cache[1]),  # ty: ignore[invalid-argument-type]
        (TypeError, lambda: 1 in cache),
        (TypeError, lambda: cache.set(1, 'v')),  # ty: ignore[invalid-argument-type]
        (TypeError, lambda: cache.delete(1)),  # ty: ignore[invalid-argument-type]
        (TypeError, lambda: cache.pop(1)),  # ty: ignore[invalid-argument-type]
        (TypeError, lambda: cache.set('k', object())),
        (TypeError, lambda: cache.add('k', object())),
        (TypeError, lambda: cache.set('k', 'v', expire=True)),
        (TypeError, lambda: cache.touch('k', expire=True)),
        (TypeError, lambda: cache.incr('text')),
        (TypeError, lambda: cache.decr('text')),
        (TypeError, lambda: cache.incr('n', 'one')),  # ty: ignore[invalid-argument-type]
        (OverflowError, lambda: cache.incr('big', 1, 2**63 - 1)),
        (OverflowError, lambda: cache.decr('big', -(2**63))),
        (ValueError, lambda: cache.clear(timeout=-1)),
    ]
    for error, call in calls:
        with pytest.raises(error):
            call()
    assert _total(cache._durable_stats(), 'broken') == 0
    cache.close()


def test_a_pickle_on_a_handle_without_pickle_still_raises(directory: Path) -> None:
    with Cache(directory, pickle=True) as writer:
        writer.set('k', Hooked('kept'))
    cache = Cache(directory, durable=True)
    with pytest.raises(UnsafeValueError):
        cache.get('k')
    with pytest.raises(UnsafeValueError):
        cache.pop('k')
    assert 'k' in cache
    cache.close()


def test_misuse_of_a_transaction_still_raises(directory: Path) -> None:
    # A nested transact() joins the outer one, so the misuse left is entering one twice.
    cache = Cache(directory, durable=True)
    txn = cache.transact()
    with txn, pytest.raises(RuntimeError, match='only once'):
        txn.__enter__()
    cache.close()


def test_an_incr_past_the_range_on_a_failed_call_still_raises(directory: Path) -> None:
    cache = Cache(directory, timeout=SHORT, durable=True)
    cache.set('n', 1)
    with peer_holding_the_write_lock(directory), pytest.raises(OverflowError):
        cache.incr('n', 1, 2**63 - 1)
    cache.close()


def test_sqlite_out_of_memory_is_a_memory_error() -> None:
    assert isinstance(_core._sqlite_error(7), MemoryError)  # SQLITE_NOMEM
    assert isinstance(_core._sqlite_error(13), OSError)  # SQLITE_FULL
    assert isinstance(_core._sqlite_error(5), Timeout)  # SQLITE_BUSY


# -- admin calls and the constructor raise ----------------------------------------------------------------


def test_admin_calls_raise_behind_the_peer(directory: Path) -> None:
    cache = Cache(directory, timeout=SHORT, durable=True)
    cache.set('k', 1, expire=-1)
    cache.set('other', 1)
    with peer_holding_the_write_lock(directory):
        for call in (
            lambda: cache.clear(),
            lambda: cache.expire(),
            lambda: cache.cull(),
        ):
            with pytest.raises(Timeout):
                call()
    cache.close()


def test_the_constructor_raises(tmp_path: Path) -> None:
    path = tmp_path / 'file'
    path.write_text('not a directory')
    with pytest.raises(OSError):
        Cache(path, durable=True)


# -- a held transaction -------------------------------------------------------------------------------------


def test_a_hold_starts_with_an_error_not_a_miss(directory: Path) -> None:
    cache = Cache(directory, timeout=SHORT, durable=True)
    cache.set('k', 1)
    with peer_holding_the_write_lock(directory), pytest.raises(Timeout):
        cache._hold_write_transaction().__enter__()
    cache.close()


@NOT_AS_ROOT
def test_failures_inside_a_held_transaction_raise(directory: Path) -> None:
    """Swallowing them would break the transaction's atomicity: it would commit without the write."""
    cache = Cache(directory, durable=True)
    cache.set('k', b'x' * BIG)
    path = _value_file(directory)
    with cache._hold_write_transaction():
        with unreadable(path):
            with pytest.raises(PermissionError):
                cache.get('k')
            with pytest.raises(PermissionError):
                cache['k']
            with pytest.raises(PermissionError):
                cache.pop('k')
        with unwritable_directory(directory):
            with pytest.raises(PermissionError):
                cache.set('new', b'y' * BIG)
            with pytest.raises(PermissionError):
                cache.add('new', b'y' * BIG)
    assert _total(cache._durable_stats(), 'broken') == 0
    assert cache.get('new') is None
    cache.close()


def test_an_outage_from_a_hook_inside_a_held_transaction_raises(
    directory: Path,
) -> None:
    cache = Cache(directory, pickle=True, durable=True)
    cache.set('k', Hooked('kept'))
    Hooked.hook = _raising(OSError('disk'))
    with cache._hold_write_transaction():
        with pytest.raises(OSError, match='disk'):
            cache.get('k')
        with pytest.raises(OSError, match='disk'):
            cache.pop('k')
    Hooked.hook = None
    assert cache.get('k').payload == 'kept'
    cache.close()


@NOT_AS_ROOT
def test_a_nested_call_inside_a_held_transaction_raises_too(directory: Path) -> None:
    """A hook on a value read inside the hold: the failing call runs on the lent connection."""
    cache = Cache(directory, pickle=True, durable=True)
    cache.set('spilled', b'x' * BIG)
    path = _value_file(directory)
    cache.set('k', Hooked('kept'))
    raised: list[BaseException] = []

    def nested() -> None:
        try:
            cache.get('spilled')
        except OSError as exc:
            raised.append(exc)

    Hooked.hook = nested
    with cache._hold_write_transaction(), unreadable(path):
        assert cache.get('k').payload == 'kept'
    assert isinstance(raised[0], PermissionError), raised
    cache.close()


@NOT_AS_ROOT
def test_a_hold_on_another_store_or_another_thread_doesnt_count(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', durable=True)
    other = Cache(tmp_path / 'other')
    cache.set('k', b'x' * BIG)
    with unreadable(_value_file(tmp_path / 'c')):
        with other._hold_write_transaction():
            assert cache.get('k', 'default') == 'default', 'a hold on another store'
        answers: list[Any] = []
        with cache._hold_write_transaction():
            thread = threading.Thread(target=lambda: answers.append(cache.get('k', 'default')), daemon=True)
            thread.start()
            thread.join(WATCHDOG)
        assert answers == ['default'], 'a hold on this store, by another thread'
    other.close()
    cache.close()


# -- reporting ----------------------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


def test_the_first_failure_logs_at_once_and_the_rest_are_counted(
    directory: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock()
    cache = Cache(directory, timeout=0.001, durable=True, clock=clock)
    cache.set('k', 1)
    with (
        peer_holding_the_write_lock(directory),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        assert cache.set('k', 2) is False
        [first] = _messages(caplog)
        assert first.levelno == logging.WARNING
        assert 'busy' in first.getMessage() and 'set: 1' in first.getMessage() and str(directory) in first.getMessage()
        assert 'timed out after' in first.getMessage()
        caplog.clear()
        for _ in range(5):
            cache.set('k', 2)
            cache.add('a', 2)
        cache.incr('n')
        clock.now += 59.0
        cache.set('k', 2)
        assert not _messages(caplog), 'nothing more inside the limit'
        clock.now += 1.0
        cache.set('k', 2)
        [second] = _messages(caplog)
        assert 'set: 7' in second.getMessage(), second.getMessage()
        assert 'add: 5' in second.getMessage()
        assert 'incr: 1' in second.getMessage()
        assert 'get' not in second.getMessage().split('(')[1].split(')')[0], second.getMessage()
        caplog.clear()
        clock.now += 60.0
        cache.touch('k')
        [third] = _messages(caplog)
        assert 'touch: 1' in third.getMessage() and 'set' not in third.getMessage().split('(')[1].split(')')[0], (
            'counts restart'
        )
    busy = cache._durable_stats()['busy']
    assert (busy['set'], busy['add'], busy['incr'], busy['touch']) == (8, 5, 1, 1), 'the stats keep every failure'
    cache.close()


def test_each_kind_of_failure_has_its_own_limit(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    """A timeout storm can't hide a damaged database, or either of them a lost invalidation."""
    clock = FakeClock()
    cache = Cache(directory, timeout=0.001, durable=True, clock=clock)
    cache.set('k', 1)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        with peer_holding_the_write_lock(directory):
            cache.set('k', 2)
            cache.set('k', 2)
            assert cache.delete('k') is False
            assert cache.delete('k') is False
        assert len(_messages(caplog)) == 2
        for path in directory.glob('diskcache-oxide.sqlite*'):
            path.write_bytes(b'\xff' * path.stat().st_size)
        cache.get('k')
        cache.get('k')
    busy, lost, broken = _messages(caplog)
    assert busy.levelno == logging.WARNING and 'busy' in busy.getMessage()
    assert (
        lost.levelno == logging.ERROR and 'lost invalidations' in lost.getMessage() and 'delete: 1' in lost.getMessage()
    )
    assert 'served stale' in lost.getMessage() and 'no expiry' in lost.getMessage()
    assert broken.levelno == logging.ERROR and 'failing' in broken.getMessage() and 'get: 1' in broken.getMessage()
    stats = cache._durable_stats()
    assert stats['busy']['set'] == 2
    assert stats['lost_deletes']['delete'] == 2
    assert stats['broken']['get'] == 2
    assert stats['busy']['delete'] == 0 and stats['broken']['delete'] == 0
    cache.close()


def test_a_clock_that_jumps_back_cant_silence_the_log(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    cache = Cache(directory, timeout=0.001, durable=True, clock=clock)
    cache.set('k', 1)
    with (
        peer_holding_the_write_lock(directory),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        cache.set('k', 2)
        clock.now -= 3600.0
        cache.set('k', 2)
    assert len(_messages(caplog)) == 2
    cache.close()


def test_the_limit_is_per_handle(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    one = Cache(directory, timeout=0.001, durable=True)
    two = Cache(directory, timeout=0.001, durable=True)
    one.set('k', 1)
    with (
        peer_holding_the_write_lock(directory),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        one.set('k', 2)
        two.set('k', 2)
    assert len(_messages(caplog)) == 2
    assert one._durable_stats()['busy']['set'] == 1
    assert two._durable_stats()['busy']['set'] == 1
    one.close()
    two.close()


def test_a_storm_on_many_threads_logs_once_and_counts_everything(
    directory: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock()
    cache = Cache(directory, timeout=0.0, durable=True, clock=clock)
    cache.set('k', 1)
    threads, each = 8, 50

    def storm() -> None:
        for _ in range(each):
            assert cache.set('k', 2) is False

    with (
        peer_holding_the_write_lock(directory),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        workers = [threading.Thread(target=storm, daemon=True) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(WATCHDOG)
            assert not worker.is_alive()
    assert len(_messages(caplog)) == 1
    assert cache._durable_stats()['busy']['set'] == threads * each
    cache.close()


@pytest.mark.filterwarnings('ignore::pytest.PytestUnraisableExceptionWarning')
def test_a_logging_handler_that_fails_doesnt_take_the_call_down(
    directory: Path,
) -> None:
    class Failing(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise ValueError('handler failed')

        def handleError(self, record: logging.LogRecord) -> None:
            raise

    cache = Cache(directory, timeout=0.001, durable=True)
    cache.set('k', 1)
    logger = logging.getLogger(LOGGER)
    handler = Failing()
    logger.addHandler(handler)
    try:
        with peer_holding_the_write_lock(directory):
            assert cache.set('k', 2) is False
    finally:
        logger.removeHandler(handler)
    cache.close()


@pytest.mark.parametrize('error', [KeyboardInterrupt(), MemoryError()], ids=lambda e: type(e).__name__)
def test_what_a_logging_handler_raises_that_must_be_seen(directory: Path, error: BaseException) -> None:
    class Failing(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise error

        def handleError(self, record: logging.LogRecord) -> None:
            raise

    cache = Cache(directory, timeout=0.001, durable=True)
    cache.set('k', 1)
    logger = logging.getLogger(LOGGER)
    handler = Failing()
    logger.addHandler(handler)
    try:
        with peer_holding_the_write_lock(directory), pytest.raises(type(error)):
            cache.set('k', 2)
    finally:
        logger.removeHandler(handler)
    cache.close()


@pytest.mark.parametrize(
    ('error', 'bucket'),
    [
        (Timeout('nested'), 'busy'),
        (TimeoutError('nested'), 'busy'),
        (OSError('disk'), 'broken'),
        (RecursionError(), 'broken'),
    ],
    ids=lambda v: type(v).__name__ if isinstance(v, BaseException) else v,
)
def test_a_hook_outage_is_counted_with_no_line_of_its_own(
    directory: Path, caplog: pytest.LogCaptureFixture, error: BaseException, bucket: str
) -> None:
    cache = Cache(directory, pickle=True, durable=True)
    cache.set('k', Hooked('kept'))
    Hooked.hook = _raising(error)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        for _ in range(5):
            assert cache.get('k', 'default') == 'default'
        assert cache.pop('k', 'default') == 'default'
    assert len(_messages(caplog)) == 1, 'the one decode report, for the one stored version'
    other = 'broken' if bucket == 'busy' else 'busy'
    stats = cache._durable_stats()
    assert (stats[bucket]['get'], stats[bucket]['pop']) == (5, 1), stats
    assert _total(stats, other) == 0, stats
    cache.close()


def test_a_hook_outage_isnt_counted_when_it_raises(directory: Path) -> None:
    cache = Cache(directory, pickle=True, durable=True)
    cache.set('k', Hooked('kept'))
    Hooked.hook = _raising(OSError('disk'))
    with cache._hold_write_transaction(), pytest.raises(OSError, match='disk'):
        cache.get('k')
    assert _total(cache._durable_stats(), 'broken') == 0
    cache.close()
    explicit = Cache(directory, pickle=True, durable=True, on_decode_error='raise')
    with pytest.raises(OSError, match='disk'):
        explicit.get('k')
    assert _total(explicit._durable_stats(), 'broken') == 0
    explicit.close()


def test_a_hook_outage_reaches_the_next_line(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    cache = Cache(directory, timeout=0.001, pickle=True, durable=True, clock=clock)
    cache.set('k', Hooked('kept'))
    with (
        peer_holding_the_write_lock(directory),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        cache.set('other', 1)
        Hooked.hook = _raising(Timeout('nested'))
        cache.get('k')
        Hooked.hook = None
        caplog.clear()
        clock.now += 60.0
        cache.set('other', 1)
    [line] = _messages(caplog)
    assert 'get: 1' in line.getMessage() and 'set: 1' in line.getMessage(), line.getMessage()
    cache.close()


def test_a_nan_from_the_clock_cant_silence_the_log(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    cache = Cache(directory, timeout=0.001, durable=True, clock=clock)
    cache.set('k', 1)
    with (
        peer_holding_the_write_lock(directory),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        clock.now = float('nan')
        cache.set('k', 2)
        clock.now = 1_000_000.0
        for _ in range(5):
            cache.set('k', 2)
            clock.now += 100.0
    assert len(_messages(caplog)) == 6, [r.getMessage() for r in _messages(caplog)]
    cache.close()


@NOT_AS_ROOT
def test_a_stale_reading_from_a_call_that_waited_isnt_a_clock_jump(
    directory: Path, caplog: pytest.LogCaptureFixture
) -> None:
    clock = FakeClock()
    # These failures come from a value file, so they're immediate whatever the handle's timeout is. It
    # only sets how old a reading can be.
    cache = Cache(directory, timeout=30.0, durable=True, clock=clock)
    cache.set('k', b'x' * BIG)
    with (
        unreadable(_value_file(directory)),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        cache.get('k')
        assert len(_messages(caplog)) == 1
        clock.now -= 89.0  # Inside LOG_EVERY + timeout.
        cache.get('k')
        assert len(_messages(caplog)) == 1, 'a step back of that size is an old reading'
        clock.now += 89.0
        clock.now -= 91.0  # Past it: the clock really jumped.
        cache.get('k')
        assert len(_messages(caplog)) == 2
    cache.close()


def test_close_logs_what_the_limit_left_out(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    clock = FakeClock()
    cache = Cache(directory, timeout=0.001, durable=True, clock=clock)
    cache.set('k', 1)
    with (
        peer_holding_the_write_lock(directory),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        for _ in range(4):
            cache.delete('k')
            cache.set('k', 2)
        first_lines = _messages(caplog)
        assert len(first_lines) == 2
        for line in first_lines:
            assert 'More may follow' in line.getMessage() and ': 1)' in line.getMessage(), line.getMessage()
        caplog.clear()
        cache.close()
    lost, busy = sorted(_messages(caplog), key=lambda r: r.levelno, reverse=True)
    assert lost.levelno == logging.ERROR and 'delete: 3' in lost.getMessage(), lost.getMessage()
    assert busy.levelno == logging.WARNING and 'set: 3' in busy.getMessage(), busy.getMessage()
    assert cache._durable_stats()['lost_deletes']['delete'] == 4, 'the stats keep every failure'


def test_close_logs_nothing_when_nothing_was_left_out(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    durable = Cache(directory, timeout=0.001, durable=True)
    durable.set('k', 1)
    with (
        peer_holding_the_write_lock(directory),
        caplog.at_level(logging.WARNING, logger=LOGGER),
    ):
        durable.set('k', 2)
        caplog.clear()
        durable.close()
        durable.close()
    assert not _messages(caplog)


def test_a_logging_handler_that_reads_the_cache_cant_undo_close(
    directory: Path,
) -> None:
    """D-018: once `close()` returns, this handle holds nothing, even if the flush ran a handler that
    used it."""
    cache = Cache(directory, timeout=0.001, durable=True)
    cache.set('k', 1)
    with peer_holding_the_write_lock(directory):
        cache.set('k', 2)
        cache.set('k', 2)  # Past the first line, so the flush has something to log.
    read: list[Any] = []

    class Reading(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            read.append(cache.get('k'))

    logger = logging.getLogger(LOGGER)
    handler = Reading()
    logger.addHandler(handler)
    try:
        cache.close()
    finally:
        logger.removeHandler(handler)
    assert read == [1], 'the handler ran, during close()'
    assert cache._handle_count() == 0, 'the handler reopened the handle, and close() left it open'


@pytest.mark.filterwarnings('ignore::pytest.PytestUnraisableExceptionWarning')
@pytest.mark.parametrize(
    'error',
    [ValueError('handler failed'), MemoryError(), KeyboardInterrupt()],
    ids=lambda e: type(e).__name__,
)
def test_a_failing_handler_cant_make_close_raise(directory: Path, error: BaseException) -> None:
    class Failing(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            raise error

        def handleError(self, record: logging.LogRecord) -> None:
            raise

    cache = Cache(directory, timeout=0.001, durable=True)
    cache.set('k', 1)
    with peer_holding_the_write_lock(directory):
        cache.set('k', 2)  # With no failing handler yet: the first line.
        cache.set('k', 2)
    logger = logging.getLogger(LOGGER)
    handler = Failing()
    logger.addHandler(handler)
    try:
        cache.close()
    finally:
        logger.removeHandler(handler)


OXIDE_FIRST = """
import sys
assert 'logging' not in sys.modules, 'this needs diskcache_oxide to come first'
import diskcache_oxide
import faulthandler
faulthandler.dump_traceback_later(40, exit=True)
import logging
import os
from pathlib import Path
from tests.unit.fork_timeout_scenarios import StuckFork, filled, on_thread

records = []
class Captured(logging.Handler):
    def emit(self, record):
        records.append(record)
log = logging.getLogger('diskcache_oxide')
log.addHandler(Captured())
log.propagate = False
directory = Path(sys.argv[1])
cache = filled(directory, 'c', durable=True)
with StuckFork(directory):
    result, _ = on_thread(lambda: cache.get('k', 'default'), limit=10.0)
    assert result == 'default', result
    assert len(records) == 1 and 'is busy' in records[0].getMessage(), records
cache.close()
sys.stdout.flush()
os._exit(0)
"""


def test_a_first_log_line_behind_a_stuck_fork_with_diskcache_oxide_imported_first(
    tmp_path: Path,
) -> None:
    """`logging` isn't imported until `_core` does it, so `_core`'s fork hook runs before `logging`'s,
    and logging's module lock isn't held while the fork waits (plan 09, step 1b)."""
    proc = subprocess.run(
        [sys.executable, '-c', OXIDE_FIRST, str(tmp_path / 's')],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert proc.returncode == 0, proc.stderr


# -- validation, NullCache and Caches -------------------------------------------------------------------------


@pytest.mark.parametrize('value', [1, 0, 'yes', None, [True]], ids=repr)
def test_durable_is_bool_only(directory: Path, value: Any) -> None:
    with pytest.raises(TypeError, match='durable|bool|PyBool'):
        Cache(directory, durable=value)
    with pytest.raises(TypeError, match='durable must be a bool'):
        NullCache(durable=value)


def test_a_null_cache_accepts_durable_and_ignores_it() -> None:
    for durable in (True, False):
        cache = NullCache(durable=durable)
        assert cache.set('k', 1) is True
        assert cache.add('k', 1) is True, 'claims succeed here and fail closed on a durable Cache'
        assert cache.get('k', 'default') == 'default'


def test_a_null_cache_takes_on_decode_error_none() -> None:
    NullCache(on_decode_error=None)
    with pytest.raises(ValueError, match='on_decode_error'):
        NullCache(on_decode_error='ignore')  # ty: ignore[invalid-argument-type]


def test_caches_passes_durable_through(tmp_path: Path) -> None:
    caches: Caches = Caches(tmp_path, timeout=SHORT, durable=True)
    caches.register('durable')
    caches.register('strict', durable=False)
    durable, strict = caches['durable'], caches['strict']
    durable.set('k', 1)
    strict.set('k', 1)
    with peer_holding_the_write_lock(tmp_path / 'durable'):
        assert durable.set('k', 2) is False
    with peer_holding_the_write_lock(tmp_path / 'strict'), pytest.raises(Timeout):
        strict.set('k', 2)
    caches.close_all()


def test_caches_defaults_decode_errors_to_miss_when_durable(tmp_path: Path) -> None:
    caches: Caches = Caches(tmp_path, durable=True)
    caches.register('c')
    caches['c'].set('k', {'a': 1})
    _sql(tmp_path / 'c', "UPDATE cache SET value = X'c1' WHERE key = 'k'")
    assert caches['c'].get('k', 'default') == 'default'
    caches.close_all()


def test_caches_refuses_a_durable_that_isnt_a_bool(tmp_path: Path) -> None:
    caches: Caches = Caches(tmp_path, durable=1)
    caches.register('c')
    with pytest.raises(TypeError):
        caches['c']


# -- what needs a fresh process ---------------------------------------------------------------------------------


@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_scenario(scenario: str, tmp_path: Path) -> None:
    proc = subprocess.run(
        [
            sys.executable,
            '-m',
            'tests.unit.durable_scenarios',
            scenario,
            str(tmp_path / 's'),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert proc.returncode == 0, proc.stderr
