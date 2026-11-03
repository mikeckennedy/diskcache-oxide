"""`Caches`: the registry of named caches (plan 08, step 3)."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache, Caches, CacheStats, FormatMismatchError, NullCache


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


def test_a_future_format_is_a_null_cache_reported_by_the_default_logger(
    base: Path, caplog: pytest.LogCaptureFixture
) -> None:
    _future_format(base / 'a')
    caches = Caches(base)
    caches.register('a')
    with caplog.at_level(logging.ERROR, logger='diskcache_oxide'):
        assert isinstance(caches['a'], NullCache)
    [record] = [r for r in caplog.records if r.name == 'diskcache_oxide']
    assert record.exc_info is not None and isinstance(record.exc_info[1], FormatMismatchError)
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
    script = 'import sqlite3, sys\ncon = sqlite3.connect(sys.argv[1])\ncon.execute("UPDATE cache SET value = X\'c1\'")\ncon.commit()'
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
def test_a_child_forked_during_lookups_can_use_the_registry(base: Path) -> None:
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
