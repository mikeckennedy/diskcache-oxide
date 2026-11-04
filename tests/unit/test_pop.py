"""`Cache.pop` decodes before it deletes (plan 09, step 1; D-026).

A value `pop` can't read stays for whoever can, a transient error costs nothing, and a value still
goes to exactly one caller: the delete is a compare-and-delete of the version that was decoded.
"""

from __future__ import annotations

import logging
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache, CorruptValueError, ForeignValueError, Timeout

from tests.conformance.proc_workers import SqlitePeer

LOGGER = 'diskcache_oxide'
BIG = 70_000  # over the 64 KiB spill threshold
# spill::OPTIMISTIC_ATTEMPTS
ATTEMPTS = 8
FOREIGN_TAG = f"X'81a5{b'$nope'.hex()}01'"  # {"$nope": 1}: a known mode with an unknown tag.
POLICIES = pytest.mark.parametrize('policy', ['raise', 'miss'])


class Hooked:
    """Unpickling it calls `Hooked.hook`, if one is set."""

    hook: Callable[[], object] | None = None

    def __init__(self, payload: str = 'x') -> None:
        self.payload = payload

    def __setstate__(self, state: dict[str, Any]) -> None:
        self.__dict__.update(state)
        if Hooked.hook is not None:
            Hooked.hook()


class Unpicklable:
    """Unpickling it always raises: a value no build can read."""

    def __init__(self, padding: str = 'x') -> None:
        self.padding = padding

    def __setstate__(self, state: dict[str, Any]) -> None:
        raise ValueError('broken __setstate__')


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


def _stored(directory: Path, key: str) -> bool:
    with Cache(directory) as other:
        return key in other


def _value_files(directory: Path) -> list[Path]:
    return list(directory.rglob('*.val'))


# -- the row stays when pop can't read the value ----------------------------------------------------


@POLICIES
def test_a_foreign_tag_survives_pop_and_a_later_build_can_still_take_it(directory: Path, policy: Any) -> None:
    cache = Cache(directory, on_decode_error=policy)
    cache.set('k', {'a': 1})
    _sql(directory, f"UPDATE cache SET value = {FOREIGN_TAG} WHERE key = 'k'")
    if policy == 'raise':
        with pytest.raises(ForeignValueError):
            cache.pop('k')
    else:
        assert cache.pop('k', 'default') == 'default'
    assert _stored(directory, 'k')
    # What a build that knows the tag sees: the row, whole, still there.
    _sql(directory, "UPDATE cache SET value = X'81a161' || X'01' WHERE key = 'k'")
    assert cache.pop('k') == {'a': 1}
    assert not _stored(directory, 'k')
    cache.close()


@POLICIES
def test_a_spilled_foreign_value_keeps_its_row_and_file(directory: Path, policy: Any) -> None:
    cache = Cache(directory, on_decode_error=policy)
    cache.set('k', b'x' * BIG)
    _sql(directory, "UPDATE cache SET mode = 99 WHERE key = 'k'")
    files = _value_files(directory)
    if policy == 'raise':
        with pytest.raises(ForeignValueError):
            cache.pop('k')
    else:
        assert cache.pop('k') is None
    assert _stored(directory, 'k')
    assert _value_files(directory) == files
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
    ids=lambda e: type(e).__name__,
)
@POLICIES
def test_a_hook_raising_a_transient_error_keeps_the_row(directory: Path, policy: Any, error: BaseException) -> None:
    cache = Cache(directory, pickle=True, on_decode_error=policy)
    cache.set('k', Hooked('kept'))

    def fail() -> None:
        raise error

    Hooked.hook = fail
    with pytest.raises(type(error)):
        cache.pop('k')
    Hooked.hook = None
    assert _stored(directory, 'k')
    assert cache.pop('k').payload == 'kept', 'the transient error did not cost the value'
    assert not _stored(directory, 'k')
    cache.close()


# -- an unreadable value still goes ---------------------------------------------------------------


@pytest.mark.parametrize('spilled', [False, True], ids=['inline', 'spilled'])
def test_an_unreadable_value_is_removed_then_raised_under_raise(directory: Path, spilled: bool) -> None:
    with Cache(directory, pickle=True) as cache:
        cache.set('k', Unpicklable('x' * (BIG if spilled else 1)))
        with pytest.raises(ValueError, match='broken __setstate__'):
            cache.pop('k')
    assert not _stored(directory, 'k')
    assert not _value_files(directory)


def test_an_unreadable_value_is_removed_and_reported_after_the_delete_under_miss(
    directory: Path,
) -> None:
    cache: Cache
    seen: list[tuple[str, bool]] = []

    def report(key: str, exc: Exception) -> None:
        seen.append((key, key in cache))

    cache = Cache(directory, on_decode_error=report)
    cache.set('k', {'a': 1})
    _sql(directory, "UPDATE cache SET value = X'c1' WHERE key = 'k'")
    assert cache.pop('k', 'default') == 'default'
    assert seen == [('k', False)], 'called once, after the row went'
    cache.close()


def test_the_miss_warning_says_the_unreadable_row_was_deleted(
    directory: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cache = Cache(directory, on_decode_error='miss')
    cache.set('k', {'a': 1})
    _sql(directory, "UPDATE cache SET value = X'c1' WHERE key = 'k'")
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert cache.pop('k') is None
    [record] = [r for r in caplog.records if r.name == LOGGER]
    assert '(deleted)' in record.getMessage()
    cache.close()


@POLICIES
def test_a_failed_delete_of_an_unreadable_value_raises_with_the_decode_error_as_its_cause(
    directory: Path, policy: Any
) -> None:
    cache = Cache(directory, timeout=0.2, on_decode_error=policy)
    cache.set('k', {'a': 1})
    _sql(directory, "UPDATE cache SET value = X'c1' WHERE key = 'k'")
    with SqlitePeer(_db(directory)) as peer:
        peer.begin('IMMEDIATE')
        with pytest.raises(Timeout) as info:
            cache.pop('k')
        peer.rollback()
    assert isinstance(info.value.__cause__, CorruptValueError)
    assert _stored(directory, 'k')
    cache.close()


def test_an_unreadable_value_a_peer_replaced_first_is_read_again(
    directory: Path,
) -> None:
    # The delete is of the version that failed, so the peer's value isn't lost, and pop returns it.
    with Cache(directory, pickle=True) as writer:
        writer.set('k', Unpicklable())
    peer = Cache(directory)
    replaced: list[bool] = []
    original = Unpicklable.__setstate__

    def replace_then_fail(self: Unpicklable, state: dict[str, Any]) -> None:
        replaced.append(True)
        peer.set('k', 'fresh')
        original(self, state)

    Unpicklable.__setstate__ = replace_then_fail
    try:
        cache = Cache(directory, pickle=True)
        assert cache.pop('k') == 'fresh'
    finally:
        Unpicklable.__setstate__ = original
    assert replaced == [True]
    assert not _stored(directory, 'k')
    cache.close()
    peer.close()


# -- losing to other writers ----------------------------------------------------------------------


def test_pop_losing_to_an_overwrite_returns_the_newer_value(directory: Path) -> None:
    cache = Cache(directory, pickle=True)
    cache.set('k', Hooked('old'))
    Hooked.hook = lambda: cache.set('k', 'new')
    assert cache.pop('k') == 'new', 'the delete of the old version lost, so it read again'
    assert 'k' not in cache
    cache.close()


def test_pop_losing_to_a_delete_is_a_miss(directory: Path) -> None:
    cache = Cache(directory, pickle=True)
    cache.set('k', Hooked('old'))
    Hooked.hook = lambda: (cache.delete('k'), None)[1]
    assert cache.pop('k', 'default') == 'default', 'the other caller removed it, so this one gets no value'
    cache.close()


def test_a_hook_that_always_overwrites_its_own_key_ends_in_the_fallback(
    directory: Path,
) -> None:
    cache = Cache(directory, pickle=True, timeout=10)
    cache.set('k', Hooked('first'))
    decoded: list[int] = []

    def overwrite() -> None:
        decoded.append(1)
        if len(decoded) <= ATTEMPTS:
            cache.set('k', Hooked(f'after {len(decoded)}'))

    Hooked.hook = overwrite
    started = time.monotonic()
    assert cache.pop('k').payload == f'after {ATTEMPTS}', 'the fallback removed and returned the last one'
    assert time.monotonic() - started < 5
    assert len(decoded) == ATTEMPTS + 1, 'one decode per attempt, and one after the locked removal'
    assert 'k' not in cache
    cache.close()


def test_only_the_fallback_can_destroy_a_foreign_tag(directory: Path, caplog: pytest.LogCaptureFixture) -> None:
    # After every optimistic attempt lost, the locked removal comes before the decode.
    cache = Cache(directory, pickle=True, on_decode_error='miss')
    cache.set('k', Hooked('first'))
    decoded: list[int] = []

    def overwrite() -> None:
        decoded.append(1)
        if len(decoded) < ATTEMPTS:
            cache.set('k', Hooked('again'))
        else:
            cache.set('k', {'a': 1})
            _sql(directory, f"UPDATE cache SET value = {FOREIGN_TAG} WHERE key = 'k'")

    Hooked.hook = overwrite
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        assert cache.pop('k', 'default') == 'default'
    assert not _stored(directory, 'k')
    [record] = [r for r in caplog.records if r.name == LOGGER]
    assert 'deleted' in record.getMessage() and 'ForeignValueError' in record.getMessage()
    cache.close()


def test_a_hook_error_during_the_fallback_costs_the_value(directory: Path) -> None:
    # The locked removal comes before the decode, so here a transient error can't keep the row.
    cache = Cache(directory, pickle=True, on_decode_error='miss')
    cache.set('k', Hooked('first'))
    decoded: list[int] = []

    def overwrite_then_fail() -> None:
        decoded.append(1)
        if len(decoded) <= ATTEMPTS:
            cache.set('k', Hooked('again'))
        else:
            raise Timeout('from a hook')

    Hooked.hook = overwrite_then_fail
    with pytest.raises(Timeout, match='from a hook'):
        cache.pop('k')
    assert len(decoded) == ATTEMPTS + 1
    assert not _stored(directory, 'k')
    cache.close()


def test_the_clock_is_read_once_however_many_times_pop_retries(directory: Path) -> None:
    reads: list[int] = []

    def clock() -> float:
        reads.append(1)
        return time.time()

    cache = Cache(directory, pickle=True, clock=clock)
    cache.set('k', Hooked('old'))
    other = Cache(directory)
    Hooked.hook = lambda: other.set('k', 'new')
    reads.clear()
    assert cache.pop('k') == 'new'
    assert len(reads) == 1
    cache.close()
    other.close()


# -- one deadline for the whole call --------------------------------------------------------------


def test_time_spent_in_a_hook_comes_out_of_the_timeout_for_the_delete(
    directory: Path,
) -> None:
    timeout, hook = 2.0, 1.5
    cache = Cache(directory, timeout=timeout, pickle=True)
    cache.set('k', Hooked('kept'))
    Hooked.hook = lambda: time.sleep(hook)
    with SqlitePeer(_db(directory)) as peer:
        peer.begin('IMMEDIATE')
        started = time.monotonic()
        with pytest.raises(Timeout, match=r'after 2(\.0)? s'):
            cache.pop('k')
        elapsed = time.monotonic() - started
        peer.rollback()
    assert timeout - 0.1 < elapsed < timeout + 1, f'{elapsed:.2f}s, not one timeout across the whole call'
    assert _stored(directory, 'k')
    cache.close()


def test_a_race_lost_after_the_deadline_times_out_with_the_row_kept(
    directory: Path,
) -> None:
    # 8 attempts at a full timeout each would run this hook far more often.
    cache = Cache(directory, timeout=0.5, pickle=True)
    cache.set('k', Hooked('first'))
    decoded: list[int] = []

    def slow_overwrite() -> None:
        decoded.append(1)
        time.sleep(0.3)
        cache.set('k', Hooked('again'))

    Hooked.hook = slow_overwrite
    with pytest.raises(Timeout, match=r'after 0\.5 s'):
        cache.pop('k')
    assert len(decoded) <= 2, 'no decode once the time is gone, and no fallback'
    Hooked.hook = None
    assert cache.pop('k').payload == 'again', "the row is the other writer's, and it stayed"
    cache.close()


def test_a_foreign_value_survives_a_single_race_lost_after_the_deadline(
    directory: Path,
) -> None:
    # Going to the fallback here would remove the row before decoding it, after just one lost race.
    cache = Cache(directory, timeout=0.05, pickle=True, on_decode_error='miss')
    cache.set('k', Hooked('first'))

    def overwrite_with_foreign_value() -> None:
        time.sleep(0.2)
        cache.set('k', {'a': 1})
        _sql(directory, f"UPDATE cache SET value = {FOREIGN_TAG} WHERE key = 'k'")

    Hooked.hook = overwrite_with_foreign_value
    with pytest.raises(Timeout):
        cache.pop('k')
    assert _stored(directory, 'k')
    cache.close()


def test_pop_waits_for_the_write_lock_and_times_out_with_the_row_kept(
    directory: Path,
) -> None:
    cache = Cache(directory, timeout=0.2)
    cache.set('k', 'v')
    with SqlitePeer(_db(directory)) as peer:
        peer.begin('IMMEDIATE')
        with pytest.raises(Timeout):
            cache.pop('k')
        peer.rollback()
    assert cache.pop('k') == 'v'
    cache.close()


# -- what a caller can see of the new order (D-026) -------------------------------------------------


def test_a_hook_sees_its_key_still_present(directory: Path) -> None:
    cache = Cache(directory, pickle=True)
    cache.set('k', Hooked())
    seen: list[bool] = []
    Hooked.hook = lambda: seen.append('k' in cache)
    cache.pop('k')
    assert seen == [True]
    assert 'k' not in cache
    cache.close()


def test_pop_of_a_missing_key_never_waits_for_a_writer(directory: Path) -> None:
    cache = Cache(directory, timeout=30)
    cache.set('other', 1)
    with SqlitePeer(_db(directory)) as peer:
        peer.begin('IMMEDIATE')
        started = time.monotonic()
        assert cache.pop('missing', 'default') == 'default'
        assert time.monotonic() - started < 2
        peer.rollback()
    cache.close()


def test_a_hook_that_pops_its_own_key_recurses_to_recursion_error(
    directory: Path,
) -> None:
    cache = Cache(directory, pickle=True)
    cache.set('k', Hooked('kept'))
    Hooked.hook = lambda: cache.pop('k')
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(80)  # Every level holds a connection, so don't open a thousand of them.
    try:
        with pytest.raises(RecursionError):
            cache.pop('k')
    finally:
        sys.setrecursionlimit(limit)
    Hooked.hook = None
    assert cache.pop('k').payload == 'kept', 'nothing was deleted on the way up'
    cache.close()


# -- value files ------------------------------------------------------------------------------------


def test_popping_a_spilled_value_returns_it_and_removes_its_file(
    directory: Path,
) -> None:
    cache = Cache(directory)
    cache.set('k', b'x' * BIG)
    assert len(_value_files(directory)) == 1
    assert cache.pop('k') == b'x' * BIG
    assert not _value_files(directory)
    assert len(cache) == 0
    cache.close()


def test_a_missing_value_file_is_a_miss_and_its_row_goes(directory: Path) -> None:
    cache = Cache(directory)
    cache.set('k', b'x' * BIG)
    _value_files(directory)[0].unlink()
    assert cache.pop('k', 'default') == 'default'
    assert len(cache) == 0
    cache.close()


def test_under_contention_a_missing_value_file_is_a_miss_and_its_row_can_stay(
    directory: Path,
) -> None:
    # As with get (D-013): the read doesn't wait to clean up. A later pop, or a write's cleanup, does.
    cache = Cache(directory, timeout=30)
    cache.set('k', b'x' * BIG)
    _value_files(directory)[0].unlink()
    with SqlitePeer(_db(directory)) as peer:
        peer.begin('IMMEDIATE')
        started = time.monotonic()
        assert cache.pop('k', 'default') == 'default'
        assert time.monotonic() - started < 2, 'pop waited to remove a row with no value'
        peer.rollback()
    assert len(cache) == 1, 'the row stayed'
    assert cache.pop('k', 'default') == 'default'
    assert len(cache) == 0, 'and the next pop removed it'
    cache.close()


# -- a held transaction ---------------------------------------------------------------------------


@pytest.mark.parametrize('commit', [True, False])
def test_pop_in_a_held_transaction_uses_it_for_the_read_and_the_delete(directory: Path, commit: bool) -> None:
    cache = Cache(directory, timeout=2)
    cache.set('k', 'v')

    class Rollback(Exception):
        pass

    try:
        with cache._hold_write_transaction():
            cache.set('k', 'written in the transaction')
            assert cache.pop('k') == 'written in the transaction', 'the read saw the uncommitted write'
            assert 'k' not in cache
            if not commit:
                raise Rollback
    except Rollback:
        pass
    assert cache.get('k') == (None if commit else 'v')
    cache.close()
