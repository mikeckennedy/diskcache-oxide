"""`Caches`: the registry of named caches (plan 08, step 3)."""

from __future__ import annotations

import inspect
import logging
import os
import subprocess
import sys
import threading
import time
import traceback
import warnings
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import (
    Cache,
    Caches,
    CacheStats,
    FanoutCache,
    FormatMismatchError,
    LayoutMismatchError,
    NullCache,
    Timeout,
)
from diskcache_oxide import caches as caches_module
from diskcache_oxide._core import _open_fanout_keeping_stored

from tests.platforms import unix_only
from tests.unit.spawn_race import race

MiB = 1 << 20


class Clock:
    """A monotonic clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return tmp_path / 'caches'


def _future_format(directory: Path) -> None:
    """Leaves a database at `directory` stamped with a format version from the future."""
    Cache(directory).close()
    script = (
        'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\ncon.execute("PRAGMA user_version = 999")\ncon.close()'
    )
    subprocess.run(
        [sys.executable, '-c', script, str(directory / 'diskcache-oxide.sqlite')],
        check=True,
    )


# -- registration and lookup ----------------------------------------------------------------------


def test_a_cache_opens_on_first_lookup_and_is_reused(base: Path) -> None:
    caches = Caches(base)
    caches.register('general')
    assert not (base / 'general').exists(), 'registering opens nothing'
    general = caches['general']
    assert isinstance(general, Cache)
    assert general.directory == str(base / 'general')
    assert caches['general'] is general
    general.set('k', 'v')
    assert caches['general'].get('k') == 'v'
    caches.close_all()


def test_settings_layer_over_the_defaults(base: Path) -> None:
    caches = Caches(base, timeout=1.0, size_limit=2**21)
    caches.register('plain')
    caches.register('big', size_limit=2**30)
    caches.register('elsewhere', directory=base.parent / 'other')
    assert caches['plain'].timeout == 1.0 and caches['plain'].size_limit == 2**21
    assert caches['big'].timeout == 1.0 and caches['big'].size_limit == 2**30
    assert caches['elsewhere'].directory == str(base.parent / 'other')
    assert caches.directory('elsewhere') == str(base.parent / 'other')
    assert list(caches) == ['plain', 'big', 'elsewhere'] and len(caches) == 3
    assert 'plain' in caches and 'nope' not in caches
    caches.close_all()


def test_a_relative_base_dir_is_made_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    caches = Caches('rel')
    caches.register('a')
    monkeypatch.chdir('/')
    assert caches.directory('a') == str(tmp_path / 'rel' / 'a')


def test_unknown_settings_fail_early(base: Path) -> None:
    with pytest.raises(TypeError, match='size_limt'):
        Caches(base, size_limt=1)
    caches = Caches(base)
    with pytest.raises(TypeError, match='serialiser'):
        caches.register('a', serialiser='json')
    with pytest.raises(ValueError):
        Caches(base, retry_open_after=-1)
    with pytest.raises(TypeError):
        Caches(base, retry_open_after=True)


def test_a_decode_reporter_must_take_the_cache_name(base: Path) -> None:
    def old_style(key: str, exc: Exception) -> None:
        pass

    with pytest.raises(TypeError, match='name, key, exc'):
        Caches(base, on_decode_error=old_style)
    caches = Caches(base)
    with pytest.raises(TypeError, match='name, key, exc'):
        caches.register('a', on_decode_error=old_style)
    caches.register('b', on_decode_error=lambda name, key, exc: None)
    caches.register('c', on_decode_error=print)  # No signature to check: accepted.


@pytest.mark.parametrize('name', ['', '.', '..', 'a/b', '../escape', '/abs'])
def test_a_name_must_be_a_plain_directory_name(base: Path, name: str) -> None:
    caches = Caches(base)
    with pytest.raises(ValueError):
        caches.register(name)
    if name:
        caches.register(name, directory=base / 'ok')  # Fine with an explicit directory.


def test_names_and_directories_are_unique(base: Path) -> None:
    caches = Caches(base)
    caches.register('a')
    with pytest.raises(ValueError, match='already registered'):
        caches.register('a')
    with pytest.raises(ValueError, match='share'):
        caches.register('b', directory=base / 'a')


def test_unknown_names_raise_key_error(base: Path) -> None:
    caches = Caches(base)
    for call in (
        lambda: caches['nope'],
        lambda: caches.disable('nope'),
        lambda: caches.enable('nope'),
        lambda: caches.enabled('nope'),
        lambda: caches.clear('nope'),
        lambda: caches.directory('nope'),
    ):
        with pytest.raises(KeyError):
            call()


def test_racing_lookups_get_one_object(base: Path) -> None:
    caches = Caches(base)
    caches.register('a')
    start = threading.Barrier(16)
    seen: list[Any] = []

    def look() -> None:
        start.wait()
        seen.append(caches['a'])

    threads = [threading.Thread(target=look) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len({id(c) for c in seen}) == 1
    caches.close_all()


def test_a_switch_during_a_lookup_never_sticks(base: Path) -> None:
    # A disable() landing between the lookup's switch check and its store must not leave the real
    # cache stored for good. The set below runs disable() from inside that first check.
    caches = Caches(base)
    caches.register('a')

    class DisableDuringCheck(set[str]):
        armed = True

        def __contains__(self, name: object) -> bool:
            answer = super().__contains__(name)
            if self.armed:
                self.armed = False
                caches.disable('a')  # Adds 'a' and pops _resolved, which is still empty.
            return answer

    caches._disabled = DisableDuringCheck()
    caches['a']  # Raced: may return the real cache this once.
    assert not caches.enabled('a')
    assert isinstance(caches['a'], NullCache), 'the next lookup follows the switch'
    caches.close_all()


def test_close_all_during_a_lookup_never_leaves_an_untracked_handle(base: Path) -> None:
    caches = Caches(base)
    caches.register('a')

    class CloseDuringCheck(set[str]):
        armed = True

        def __contains__(self, name: object) -> bool:
            if self.armed:
                self.armed = False
                caches.close_all()
            return super().__contains__(name)

    caches._disabled = CloseDuringCheck()
    caches['a']
    assert 'a' not in caches._resolved or caches._resolved['a'] is caches._opened.get('a')
    second = caches['a']
    assert second is caches._opened['a'], 'what lookups return is what close_all will close'
    caches.close_all()


# -- open failures --------------------------------------------------------------------------------


def test_a_cache_that_cant_open_is_a_null_cache_and_is_retried(base: Path) -> None:
    clock = Clock()
    reports: list[tuple[str, Exception]] = []
    caches = Caches(
        base,
        on_open_error=lambda name, exc: reports.append((name, exc)),
        _monotonic=clock,
    )
    caches.register('a')
    base.mkdir()
    (base / 'a').write_text('a file where the directory should be')

    first = caches['a']
    assert isinstance(first, NullCache) and not first
    assert caches['a'] is first, 'remembered: no second attempt yet'
    [(name, exc)] = reports
    assert name == 'a' and isinstance(exc, OSError)

    clock.now += 9.9
    assert caches['a'] is first
    assert len(reports) == 1

    (base / 'a').unlink()
    clock.now += 0.2
    healed = caches['a']
    assert isinstance(healed, Cache), 'tried again after retry_open_after, and recovered'
    assert caches['a'] is healed
    caches.close_all()


def test_a_busy_open_is_a_null_cache_retried_within_a_second(
    base: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # Workers that start together can find the cache busy. That isn't an outage: it's a warning,
    # and the next try comes after a second, not after retry_open_after.
    clock = Clock()
    caches = Caches(base, retry_open_after=10.0, _monotonic=clock)
    caches.register('a')
    attempts: list[Path] = []

    def busy_once(path: Path, **settings: Any) -> Cache:
        attempts.append(path)
        if len(attempts) == 1:
            raise Timeout('timed out after 60 s waiting for a lock or a pending fork')
        return Cache(path, **settings)

    monkeypatch.setattr(caches_module, 'Cache', busy_once)
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        first = caches['a']
    assert isinstance(first, NullCache)
    [record] = [r for r in caplog.records if r.name == 'diskcache_oxide']
    assert record.levelno == logging.WARNING, 'busy is a warning, not an error'
    assert 'is busy' in record.getMessage() and 'retrying in 1 s' in record.getMessage()

    clock.now += 0.9
    assert caches['a'] is first
    assert len(attempts) == 1, 'no second attempt inside the second'
    clock.now += 0.2
    healed = caches['a']
    assert isinstance(healed, Cache), 'tried again after one second, not ten'
    assert len(attempts) == 2
    caches.close_all()


def test_close_all_forgets_a_failed_open(base: Path) -> None:
    clock = Clock()
    caches = Caches(base, on_open_error=lambda name, exc: None, retry_open_after=10.0, _monotonic=clock)
    caches.register('a')
    base.mkdir()
    (base / 'a').write_text('a file where the directory should be')
    assert isinstance(caches['a'], NullCache)

    (base / 'a').unlink()
    caches.close_all()
    assert isinstance(caches['a'], Cache), 'close_all forgot the failure: no wait for retry_open_after'
    caches.close_all()


def test_a_future_format_root_opens_in_its_versioned_subdirectory(base: Path, caplog: pytest.LogCaptureFixture) -> None:
    # Plan 26: another format at the root redirects instead of failing, so the registry hands out
    # a working cache and never calls on_open_error.
    _future_format(base / 'a')
    reported: list[str] = []
    caches = Caches(base, on_open_error=lambda name, exc: reported.append(name))
    caches.register('a')
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        cache = caches['a']
    assert isinstance(cache, Cache)
    assert Path(cache.directory) == base / 'a' / 'diskcache-oxide-format-3'
    cache['k'] = 'v'
    assert cache['k'] == 'v'
    assert reported == []
    caches.close_all()


def test_an_open_error_a_redirect_cannot_fix_is_a_null_cache_reported_by_the_default_logger(
    base: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Another format inside the versioned subdirectory is never redirected again: still an error. (A
    # versioned subdirectory never creates its parent, so the parent comes first.)
    (base / 'a').mkdir(parents=True)
    _future_format(base / 'a' / 'diskcache-oxide-format-3')
    caches = Caches(base)
    caches.register('a')
    with caplog.at_level(logging.ERROR, logger='diskcache_oxide'):
        assert isinstance(caches['a'], NullCache)
    [record] = [r for r in caplog.records if r.name == 'diskcache_oxide']
    assert isinstance(record.exc_info, tuple) and isinstance(record.exc_info[1], FormatMismatchError)
    assert "'a'" in record.getMessage()


def test_a_bad_setting_value_still_raises(base: Path) -> None:
    caches = Caches(base)
    caches.register('a', size_limit=True)
    caches.register('b', serializer='yaml')
    with pytest.raises(TypeError):
        caches['a']
    with pytest.raises(ValueError):
        caches['b']


def test_a_failing_open_reporter_is_logged(base: Path, caplog: pytest.LogCaptureFixture) -> None:
    def report(name: str, exc: Exception) -> None:
        raise RuntimeError('sentry is down')

    caches = Caches(base, on_open_error=report)
    caches.register('a')
    base.mkdir()
    (base / 'a').write_text('')
    with caplog.at_level(logging.ERROR, logger='diskcache_oxide'):
        assert isinstance(caches['a'], NullCache)
    assert any('on_open_error' in r.getMessage() for r in caplog.records)


# -- decode errors --------------------------------------------------------------------------------


def test_a_registry_decode_reporter_learns_which_cache(base: Path) -> None:
    calls: list[tuple[str, str, Exception]] = []
    caches = Caches(base, on_decode_error=lambda name, key, exc: calls.append((name, key, exc)))
    caches.register('a')
    caches['a'].set('k', {'x': 1})
    script = (
        'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\ncon.execute("UPDATE cache SET value = '
        "X'c1'\")\ncon.commit()"
    )
    subprocess.run(
        [sys.executable, '-c', script, str(base / 'a' / 'diskcache-oxide.sqlite')],
        check=True,
    )
    assert caches['a'].get('k') is None
    [(name, key, _)] = calls
    assert (name, key) == ('a', 'k')
    caches.close_all()


# -- switches -------------------------------------------------------------------------------------


def test_a_disabled_cache_is_null_but_still_invalidates(base: Path) -> None:
    caches = Caches(base)
    caches.register('a')
    real = caches['a']
    real.set('k', 'v')
    real.set('j', 'v')

    caches.disable('a')
    assert not caches.enabled('a')
    off = caches['a']
    assert isinstance(off, NullCache)
    assert caches['a'] is off
    assert off.get('k') is None
    off.set('new', 'v')
    assert 'new' not in real, 'writes are dropped'
    off.delete('k')
    assert 'k' not in real, 'deletes reach the real cache'
    assert caches.clear('a') == 1, 'clear() clears the real cache while disabled'

    caches.enable('a')
    assert caches.enabled('a')
    assert caches['a'] is real
    caches.close_all()


def test_a_disabled_caches_transact_is_a_no_op(base: Path) -> None:
    caches = Caches(base, timeout=0.05)
    caches.register('a')
    real = caches['a']
    real.set('k', 'v')
    caches.disable('a')
    off = caches['a']
    with pytest.raises(KeyError), off.transact() as tx:
        assert tx is off
        tx.set('new', 'v')
        tx.delete('k')
        assert real.set('elsewhere', 1) is True, "the real cache's write lock was taken"
        raise KeyError('k')
    assert 'new' not in real, 'writes are dropped'
    assert 'k' not in real, 'the delete reached the real cache and stayed'
    caches.enable('a')
    with caches['a'].transact() as tx:
        assert tx is real
        tx.set('in-tx', 1)
    assert real.get('in-tx') == 1
    caches.close_all()


# -- admin ----------------------------------------------------------------------------------------


def test_stats_covers_every_cache_and_never_raises(base: Path) -> None:
    caches = Caches(base)
    caches.register('good')
    caches.register('broken')
    caches.register('unopened')
    caches.register('off')
    caches['good'].set('k', 'v')
    base.mkdir(exist_ok=True)
    (base / 'broken').write_text('')
    caches.disable('off')

    stats = caches.stats()
    assert list(stats) == ['good', 'broken', 'unopened', 'off']
    assert stats['good'] == CacheStats('good', str(base / 'good'), True, 1, stats['good'].volume)
    assert stats['good'].volume > 0
    assert stats['broken'].error is not None and stats['broken'].count == 0
    assert stats['unopened'].error is None and stats['unopened'].count == 0
    assert stats['off'].enabled is False
    caches.close_all()


def test_admin_calls_on_a_cache_that_cant_open(base: Path, caplog: pytest.LogCaptureFixture) -> None:
    caches = Caches(base)
    caches.register('good')
    caches.register('broken')
    caches['good'].set('k', 'v', expire=0.001)
    base.mkdir(exist_ok=True)
    (base / 'broken').write_text('')
    with pytest.raises(OSError):
        caches.clear('broken')
    time.sleep(0.01)
    with caplog.at_level(logging.ERROR, logger='diskcache_oxide'):
        assert caches.expire_all() == {'good': 1, 'broken': None}
    caches.close_all()


def test_clear_and_expire_all_take_a_timeout(base: Path) -> None:
    from tests.conformance.proc_workers import SqlitePeer

    caches = Caches(base, timeout=60)
    caches.register('a')
    for i in range(5):
        caches['a'].set(f'k{i}', 'v', expire=0.001)
    time.sleep(0.01)
    with SqlitePeer(base / 'a' / 'diskcache-oxide.sqlite') as peer:
        peer.begin('IMMEDIATE')
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            caches.clear('a', timeout=0.2)
        assert caches.expire_all(timeout=0.2) == {'a': None}
        assert time.monotonic() - started < 5
        peer.rollback()
    left = len(caches['a'])  # A set may sweep rows that expired before it.
    assert caches.expire_all(timeout=5) == {'a': left}
    caches.close_all()


def test_close_all_and_the_context_manager(base: Path) -> None:
    with Caches(base) as caches:
        caches.register('a')
        first = caches['a']
        first.set('k', 'v')
        caches.close_all()
        second = caches['a']
        assert second is not first, 'reopened after close_all'
        assert second.get('k') == 'v'
        assert first.get('k') == 'v', 'a handle handed out before still works'
    assert caches['a'].get('k') == 'v'
    caches.close_all()


@pytest.mark.skipif(not hasattr(os, 'fork'), reason='needs fork')
@unix_only('fork')
def test_a_child_forked_during_lookups_can_use_the_registry(base: Path) -> None:
    assert sys.platform != 'win32'
    caches = Caches(base)
    for i in range(8):
        caches.register(f'c{i}')
    stop = threading.Event()

    def churn() -> None:
        while not stop.is_set():
            for i in range(8):
                caches[f'c{i}'].get('k')
            caches.close_all()

    threads = [threading.Thread(target=churn) for _ in range(4)]
    for t in threads:
        t.start()
    try:
        time.sleep(0.05)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore', DeprecationWarning)  # forking a threaded process
            pid = os.fork()
        if pid == 0:
            code = 1
            try:
                for i in range(8):
                    caches[f'c{i}'].set('k', i)
                code = 0 if all(caches[f'c{i}'].get('k') == i for i in range(8)) else 2
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0
    finally:
        stop.set()
        for t in threads:
            t.join()
    caches.close_all()


# -- sharded caches (plan 13, step 13h) -------------------------------------------------------------


def test_shards_registers_a_fanout_cache_read_with_fanout(base: Path) -> None:
    caches = Caches(base, timeout=1.0)
    caches.register('rate', shards=4, size_limit=8 * 2**20)
    caches.register('plain')
    rate = caches.fanout('rate')
    assert isinstance(rate, FanoutCache) and rate.shards == 4
    assert caches.fanout('rate') is rate
    rate.set('k', 1)
    assert rate.get('k') == 1
    assert isinstance(caches['plain'], Cache)
    caches.close_all()


def test_reading_a_cache_the_wrong_way_raises_type_error(base: Path) -> None:
    caches = Caches(base)
    caches.register('rate', shards=2)
    caches.register('plain')
    with pytest.raises(TypeError, match='fanout'):
        caches['rate']
    with pytest.raises(TypeError, match='shards='):
        caches.fanout('plain')
    with pytest.raises(KeyError):
        caches.fanout('missing')
    caches.close_all()


def test_shards_is_per_cache_and_settings_are_checked(base: Path) -> None:
    with pytest.raises(TypeError, match='shards'):
        Caches(base, shards=4)
    caches = Caches(base)
    with pytest.raises(TypeError, match='bogus'):
        caches.register('x', shards=2, bogus=1)
    caches.register('ok', shards=2, durable=True)


def test_a_sharded_cache_takes_the_registry_defaults(base: Path) -> None:
    seen: list[tuple[str, str]] = []
    caches = Caches(base, on_decode_error=lambda name, key, exc: seen.append((name, key)), timeout=2.0)
    caches.register('rate', shards=2)
    assert caches.fanout('rate').timeout == 2.0
    caches.close_all()


def test_a_disabled_sharded_cache_is_a_null_cache_that_still_invalidates(base: Path) -> None:
    caches = Caches(base)
    caches.register('rate', shards=2)
    real = caches.fanout('rate')
    real.set('k', 1)
    caches.disable('rate')
    off = caches.fanout('rate')
    assert isinstance(off, NullCache) and not off.enabled
    assert off.get('k') is None
    with off.transact(key='k') as tx:
        assert tx is off
    off.delete('k')
    caches.enable('rate')
    assert caches.fanout('rate').get('k') is None
    caches.close_all()


def test_a_sharded_cache_that_cant_open_is_a_null_cache_then_retried(base: Path) -> None:
    clock = Clock()
    errors: list[str] = []
    caches = Caches(base, on_open_error=lambda name, exc: errors.append(name), retry_open_after=10.0, _monotonic=clock)
    caches.register('rate', shards=2)
    (base / 'rate').parent.mkdir(parents=True, exist_ok=True)
    (base / 'rate').write_text('a file, not a directory')
    failed = caches.fanout('rate')
    assert isinstance(failed, NullCache)
    assert caches.fanout('rate') is failed and errors == ['rate']
    (base / 'rate').unlink()
    clock.now += 11
    assert isinstance(caches.fanout('rate'), FanoutCache)
    caches.close_all()


def test_shards_none_registers_a_fanout_cache_with_the_default_eight_shards(base: Path) -> None:
    caches = Caches(base)
    caches.register('rate', shards=None)
    rate = caches.fanout('rate')
    assert isinstance(rate, FanoutCache) and rate.shards == 8
    assert sorted(p.name for p in (base / 'rate').iterdir() if p.is_dir()) == [f'{i:03d}' for i in range(8)]
    caches.close_all()
    again = Caches(base)
    again.register('rate', shards=None)
    reopened = again.fanout('rate')
    assert isinstance(reopened, FanoutCache) and reopened.shards == 8, 'a root that exists keeps its stored count'
    again.close_all()


def _fanout_root(directory: Path) -> None:
    FanoutCache(directory, shards=8).close()


def _plain_on_a_root(base: Path, caches: Caches) -> tuple[str, bool]:
    _fanout_root(base / 'rl')
    caches.register('rl')
    return 'rl', False


def _plain_on_a_shard(base: Path, caches: Caches) -> tuple[str, bool]:
    _fanout_root(base / 'rl')
    caches.register('shard', directory=base / 'rl' / '000')
    return 'shard', False


@pytest.mark.parametrize('arrange', [_plain_on_a_root, _plain_on_a_shard])
def test_a_layout_mismatch_is_a_null_cache_reported_remembered_and_retried(base: Path, arrange: Any) -> None:
    clock = Clock()
    errors: list[tuple[str, Exception]] = []
    caches = Caches(
        base, on_open_error=lambda name, exc: errors.append((name, exc)), retry_open_after=10.0, _monotonic=clock
    )
    name, sharded = arrange(base, caches)

    def lookup() -> object:
        return caches.fanout(name) if sharded else caches[name]

    first = lookup()
    assert isinstance(first, NullCache)
    [(reported, exc)] = errors
    assert reported == name and isinstance(exc, LayoutMismatchError)

    clock.now += 9.9
    assert lookup() is first and len(errors) == 1, 'remembered: no second open inside the window'
    clock.now += 0.2
    retried = lookup()
    assert isinstance(retried, NullCache) and retried is not first
    assert len(errors) == 2, 'tried again after retry_open_after'
    caches.close_all()


def test_a_layout_mismatch_raised_directly_is_still_a_value_error(base: Path) -> None:
    _fanout_root(base / 'rl')
    attempts = [
        lambda: FanoutCache(base / 'rl', shards=16, raise_on_mismatch=True),
        lambda: Cache(base / 'rl'),
        lambda: Cache(base / 'rl' / '000'),
    ]
    for attempt in attempts:
        with pytest.raises(LayoutMismatchError) as caught:
            attempt()
        assert isinstance(caught.value, ValueError)
        assert isinstance(caught.value, FormatMismatchError)
        assert isinstance(caught.value, RuntimeError)
    assert LayoutMismatchError.__module__ == 'diskcache_oxide'
    assert [c.__name__ for c in LayoutMismatchError.__mro__[:4]] == [
        'LayoutMismatchError',
        'FormatMismatchError',
        'RuntimeError',
        'ValueError',
    ]


# -- a registered shard count that differs from the folder's (plan 15) ---------------------------------

MISMATCH_ERROR = 'a FanoutCache is registered with a shard count its folder does not have'


@pytest.fixture(autouse=True)
def _fresh_mismatch_reports() -> Iterator[None]:
    caches_module._forget_reported_shard_mismatches()
    yield
    caches_module._forget_reported_shard_mismatches()


def _mismatch_records(caplog: pytest.LogCaptureFixture) -> tuple[list[logging.LogRecord], list[logging.LogRecord]]:
    """The registry's WARNINGs about a shard-count mismatch, and its ERRORs."""
    records = [r for r in caplog.records if r.name == 'diskcache_oxide']
    warned = [r for r in records if r.levelno == logging.WARNING and 'is registered with shards=' in r.getMessage()]
    errored = [r for r in records if r.levelno == logging.ERROR and r.getMessage() == MISMATCH_ERROR]
    return warned, errored


def test_a_folder_with_another_shard_count_opens_with_its_own_and_warns(
    base: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with FanoutCache(base / 'rl', shards=8) as old:
        old.set('kept', 1)
    errors: list[tuple[str, Exception]] = []
    caches = Caches(base, on_open_error=lambda name, exc: errors.append((name, exc)))
    caches.register('rl', shards=4)
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        rl = caches.fanout('rl')
    assert isinstance(rl, FanoutCache) and rl.shards == 8, 'the count on disk wins'
    assert rl.get('kept') == 1, 'the entries are still there'
    assert errors == [], 'not an open failure'
    [warning], [error] = _mismatch_records(caplog)
    path = str(base / 'rl')
    assert warning.getMessage() == (
        f"cache 'rl' at {path} has 8 shards but is registered with shards=4; using the 8 on disk. "
        f'To apply shards=4, delete {path} while no process has it open'
    )
    assert error.msg == MISMATCH_ERROR and error.args == (), 'a constant template, for one error-tracker issue'
    extra = tuple(getattr(error, k) for k in ('cache', 'directory', 'stored_shards', 'registered_shards'))
    assert extra == ('rl', path, 8, 4)
    assert sorted(p.name for p in (base / 'rl').iterdir() if p.is_dir()) == [f'{i:03d}' for i in range(8)]
    caches.close_all()


def test_the_error_is_once_per_process_and_mismatch_but_the_warning_is_every_open(
    base: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _fanout_root(base / 'rl')
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        caches = Caches(base)
        caches.register('rl', shards=4)
        caches.fanout('rl')
        caches.close_all()
        caches.fanout('rl')  # A reopen.
        caches.close_all()
        rebuilt = Caches(base)  # A second registry in the same process.
        rebuilt.register('rl', shards=4)
        rebuilt.fanout('rl')
        rebuilt.close_all()
        assert [len(r) for r in _mismatch_records(caplog)] == [3, 1]
        another = Caches(base)  # A later registration with yet another count is a new mismatch.
        another.register('rl', shards=16)
        reopened = another.fanout('rl')
        assert isinstance(reopened, FanoutCache) and reopened.shards == 8
        another.close_all()
    assert [len(r) for r in _mismatch_records(caplog)] == [4, 2]


def test_racing_lookups_log_once_and_share_one_handle(
    base: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fanout_root(base / 'rl')
    caches = Caches(base)
    caches.register('rl', shards=4)
    built = threading.Barrier(8)
    handles: list[FanoutCache] = []
    real = caches_module._open_fanout_keeping_stored

    def every_thread_builds_a_handle(*args: Any, **kwargs: Any) -> FanoutCache:
        cache = real(*args, **kwargs)
        handles.append(cache)
        built.wait(10)  # All 8 have built one before any reaches `setdefault`.
        return cache

    monkeypatch.setattr(caches_module, '_open_fanout_keeping_stored', every_thread_builds_a_handle)
    start = threading.Barrier(8)
    found: list[object] = []

    def lookup() -> None:
        start.wait(10)
        found.append(caches.fanout('rl'))

    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        threads = [threading.Thread(target=lookup) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
    assert len(found) == 8 and len({id(c) for c in found}) == 1, 'every lookup got the one kept handle'
    assert len(handles) == 8, 'the race was not forced'
    assert [len(r) for r in _mismatch_records(caplog)] == [1, 1], 'a losing handle logged'
    caches.close_all()


def test_a_larger_folder_opens_with_the_default_connections_for_its_own_count(
    base: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Registered 2, stored 16: the default total for 2 shards (8) can't split over 16, so the open
    # must use the folder's count for the default, not the registration's.
    FanoutCache(base / 'rl', shards=16).close()
    errors: list[tuple[str, Exception]] = []
    caches = Caches(base, on_open_error=lambda name, exc: errors.append((name, exc)))
    caches.register('rl', shards=2)
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        rl = caches.fanout('rl')
    assert isinstance(rl, FanoutCache) and rl.shards == 16
    assert errors == []
    assert [len(r) for r in _mismatch_records(caplog)] == [1, 1]
    caches.close_all()


def test_the_error_is_once_per_folder_even_through_a_symlinked_base(
    tmp_path: Path, base: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _fanout_root(base / 'rl')
    link = tmp_path / 'link'
    link.symlink_to(base)
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        for where in (base, link):
            caches = Caches(where)
            caches.register('rl', shards=4)
            caches.fanout('rl')
            caches.close_all()
    assert [len(r) for r in _mismatch_records(caplog)] == [2, 1], 'one folder, one ERROR'


def test_a_new_folder_gets_the_registered_count_and_shards_none_never_warns(
    base: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caches = Caches(base)
    caches.register('new', shards=4)
    caches.register('rl', shards=None)
    _fanout_root(base / 'rl')
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        new, rl = caches.fanout('new'), caches.fanout('rl')
        assert isinstance(new, FanoutCache) and new.shards == 4
        assert isinstance(rl, FanoutCache) and rl.shards == 8
    assert _mismatch_records(caplog) == ([], [])
    caches.close_all()


@pytest.mark.parametrize(
    'settings', [{'size_limit': 4 * MiB}, {'max_connections': 4}], ids=['size_limit', 'max_connections']
)
def test_settings_that_fit_the_registered_count_but_not_the_folders_are_a_reported_null_cache(
    base: Path, settings: dict[str, Any]
) -> None:
    _fanout_root(base / 'rl')
    errors: list[tuple[str, Exception]] = []
    caches = Caches(base, on_open_error=lambda name, exc: errors.append((name, exc)))
    caches.register('rl', shards=4, **settings)
    assert isinstance(caches.fanout('rl'), NullCache)
    [(name, exc)] = errors
    assert name == 'rl' and isinstance(exc, LayoutMismatchError)
    message = str(exc)
    assert 'has 8 shards but shards=4 was asked for' in message, message
    assert next(iter(settings)) in message and 'delete the directory' in message, message
    caches.close_all()


@pytest.mark.parametrize(
    'settings', [{'size_limit': 3 * MiB}, {'max_connections': 3}], ids=['size_limit', 'max_connections']
)
def test_settings_wrong_for_the_registered_count_still_raise_whatever_is_on_disk(
    base: Path, settings: dict[str, Any]
) -> None:
    _fanout_root(base / 'rl')
    caches = Caches(base)
    caches.register('rl', shards=4, **settings)
    caches.register('new', shards=4, **settings)
    for name in ('rl', 'new'):
        with pytest.raises(ValueError, match=next(iter(settings))) as caught:
            caches.fanout(name)
        assert not isinstance(caught.value, LayoutMismatchError), 'a bad setting, not an outage'
    assert not (base / 'new').exists(), 'the check ran before anything was created'
    caches.close_all()


def test_admin_calls_on_a_mismatched_registration_use_the_folders_count(
    base: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with FanoutCache(base / 'rl', shards=8) as old:
        for n in range(5):
            old.set(f'k{n}', n)
    caches = Caches(base)
    caches.register('rl', shards=4)
    with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
        assert caches.stats()['rl'].count == 5
        assert caches.expire_all() == {'rl': 0}
        assert caches.clear('rl') == 5
    assert [len(r) for r in _mismatch_records(caplog)] == [1, 1], 'one real open, so one report'
    caches.close_all()


def test_a_private_parameter_is_never_a_registry_setting(base: Path) -> None:
    caches = Caches(base)
    with pytest.raises(TypeError, match='unknown Cache setting'):
        caches.register('rl', shards=4, _anything=True)
    with pytest.raises(TypeError, match='unknown Cache setting'):
        Caches(base, _anything=True)


def test_raise_on_mismatch_in_a_registration_makes_another_count_an_open_failure(base: Path) -> None:
    _fanout_root(base / 'rl')
    errors: list[tuple[str, Exception]] = []
    caches = Caches(base, on_open_error=lambda name, exc: errors.append((name, exc)))
    caches.register('rl', shards=4, raise_on_mismatch=True)
    assert isinstance(caches.fanout('rl'), NullCache)
    [(name, exc)] = errors
    assert name == 'rl' and isinstance(exc, LayoutMismatchError), exc
    caches.close_all()


def test_the_keeping_open_takes_every_fanout_setting() -> None:
    ours = inspect.signature(_open_fanout_keeping_stored).parameters
    theirs = inspect.signature(FanoutCache).parameters
    assert list(ours) == list(theirs), 'a FanoutCache setting is missing from _open_fanout_keeping_stored'
    for name, param in theirs.items():
        if name == 'shards':
            assert ours[name].default is inspect.Parameter.empty and ours[name].kind == param.kind
        else:
            assert (ours[name].kind, ours[name].default) == (param.kind, param.default), name


def _registry_racer(base: str, shards: int, barrier: Any, results: Any) -> None:
    try:
        seen: list[logging.LogRecord] = []

        class Keep(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record)

        logger = logging.getLogger('diskcache_oxide')
        logger.addHandler(Keep())
        logger.setLevel(logging.WARNING)
        caches = Caches(base)
        caches.register('rl', shards=shards)
        barrier.wait(30)
        cache = caches.fanout('rl')
        if not isinstance(cache, FanoutCache):
            results.put(('null', shards))
            return
        cache.incr('k')
        warned = sum(r.levelno == logging.WARNING and 'is registered with shards=' in r.getMessage() for r in seen)
        errored = sum(r.levelno == logging.ERROR and r.getMessage() == MISMATCH_ERROR for r in seen)
        caches.close_all()
        results.put(('ok', (shards, cache.shards, warned, errored)))
    except BaseException:
        results.put(('crash', traceback.format_exc()))


@pytest.mark.timeout(120)
def test_processes_registering_different_counts_on_a_new_folder_all_use_the_winners(base: Path) -> None:
    root = base / 'rl'
    outcomes = race([(_registry_racer, (str(base), 8 if i % 2 else 16)) for i in range(8)])
    assert all(kind == 'ok' for kind, _ in outcomes), outcomes
    reports = [detail for _, detail in outcomes]
    with FanoutCache(root) as cache:
        winner = cache.shards
        assert cache.get('k') == 8, 'every incr landed on one shard: one total'
    for registered, opened, warned, errored in reports:
        assert opened == winner, reports
        expected = 0 if registered == winner else 1
        assert (warned, errored) == (expected, expected), reports
    names = sorted(p.name for p in root.iterdir())
    assert names == sorted([*(f'{i:03d}' for i in range(winner)), 'diskcache-oxide-fanout']), names


def test_stats_clear_expire_and_close_cover_sharded_caches(base: Path) -> None:
    caches = Caches(base)
    caches.register('rate', shards=2)
    caches.register('plain')
    for n in range(10):
        caches.fanout('rate').set(f'k{n}', n)
    stats = caches.stats()
    assert stats['rate'].count == 10 and stats['rate'].error is None
    assert caches.expire_all() == {'rate': 0, 'plain': 0}
    assert caches.clear('rate') == 10
    assert len(caches.fanout('rate')) == 0
    caches.close_all()
    assert caches.fanout('rate').set('again', 1) is True
    caches.close_all()


def test_a_sharded_cache_cannot_share_a_directory_with_another(base: Path) -> None:
    caches = Caches(base)
    caches.register('rate', shards=2)
    with pytest.raises(ValueError, match='share the directory'):
        caches.register('other', directory=base / 'rate')
