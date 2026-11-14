"""Background maintenance from Python (plan 28): the setting, the thread at work, and its reports."""

from __future__ import annotations

import logging
import pickle
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from diskcache_oxide import Cache, Caches, FanoutCache, NullCache, _core

from tests.conformance.proc_workers import stored_rows
from tests.maintenance import fast_maintenance, interval_from_env

DB = 'diskcache-oxide.sqlite'
FAST = 0.1
ZERO = {'interval': 0.0, 'turns': 0, 'removed': 0, 'failures': 0, 'last_error': None}


@pytest.fixture
def fast() -> Iterator[None]:
    with fast_maintenance():
        yield


def eventually(done: Callable[[], bool], within: float = 10.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if done():
            return True
        time.sleep(0.02)
    return done()


def expiring(cache: Cache | FanoutCache, count: int, expire: float = 0.3) -> None:
    """`count` entries that expire together after `expire`, which outlasts the writes, so no write
    sweeps any of them; returns once they have."""
    for i in range(count):
        cache.set(f'k{i}', i, expire=expire)
    time.sleep(expire + 0.1)


# -- the setting --------------------------------------------------------------------------------------


@pytest.mark.races_maintenance
def test_the_default_is_five_minutes_and_zero_turns_it_off(tmp_path: Path) -> None:
    with Cache(tmp_path / 'a') as cache:
        assert cache.maintenance_stats() == {**ZERO, 'interval': 300.0}
    with Cache(tmp_path / 'b', maintenance_interval=0) as cache:
        assert cache.maintenance_stats()['interval'] == 0.0
    with FanoutCache(tmp_path / 'c', shards=2, maintenance_interval=60) as fanout:
        assert fanout.maintenance_stats()['interval'] == 60.0
    # A closed cache gets no turns.
    assert fanout.maintenance_stats()['interval'] == 0.0


@pytest.mark.parametrize(
    ('value', 'error'),
    [
        (True, TypeError),
        ('60', TypeError),
        (0.5, ValueError),
        (-1, ValueError),
        (float('nan'), ValueError),
        (86_401, ValueError),
    ],
)
def test_a_bad_interval_is_refused(tmp_path: Path, value: object, error: type[Exception]) -> None:
    with pytest.raises(error, match='maintenance_interval'):
        Cache(tmp_path / 'c', maintenance_interval=value)  # ty: ignore[invalid-argument-type]


def test_a_day_is_the_longest_interval(tmp_path: Path) -> None:
    with Cache(tmp_path / 'c', maintenance_interval=86_400) as cache:
        assert cache.maintenance_stats()['interval'] == 86_400.0


def test_a_cache_with_its_own_clock_gets_no_background_maintenance(tmp_path: Path) -> None:
    with Cache(tmp_path / 'a', clock=time.time) as cache:
        assert cache.maintenance_stats()['interval'] == 0.0
    with pytest.raises(ValueError, match='clock='):
        Cache(tmp_path / 'b', clock=time.time, maintenance_interval=60)
    Cache(tmp_path / 'c', clock=time.time, maintenance_interval=0).close()


@pytest.mark.parametrize('clocked_first', [True, False])
def test_a_clocked_handle_asking_for_none_still_vetoes_the_directory(tmp_path: Path, clocked_first: bool) -> None:
    # The handle with its own clock asks for 0, which a registry's default forces; another handle on
    # the same directory, asking for the default, must not turn maintenance back on under it.
    opens = [lambda: Cache(tmp_path / 'c', clock=time.time, maintenance_interval=0), lambda: Cache(tmp_path / 'c')]
    first, second = opens if clocked_first else opens[::-1]
    with first() as a, second() as b:
        assert a.maintenance_stats()['interval'] == b.maintenance_stats()['interval'] == 0.0


def test_the_interval_survives_pickling(tmp_path: Path) -> None:
    with Cache(tmp_path / 'c', maintenance_interval=90) as cache:
        copy = pickle.loads(pickle.dumps(cache))
        assert copy.maintenance_stats()['interval'] == 90.0


def test_a_null_cache_takes_the_setting_and_reports_nothing() -> None:
    assert NullCache(maintenance_interval=60).maintenance_stats() == ZERO
    with pytest.raises(TypeError, match='maintenance_interval'):
        NullCache(maintenance_interval=True)


def test_caches_reports_each_caches_maintenance(tmp_path: Path) -> None:
    with Caches(tmp_path, maintenance_interval=120) as caches:
        caches.register('a')
        caches.register('b', maintenance_interval=0)
        caches.register('c', clock=time.time, maintenance_interval=0)
        stats = caches.stats()
        intervals = {name: (stat.maintenance or {}).get('interval') for name, stat in stats.items()}
        assert intervals == {'a': 120.0, 'b': 0.0, 'c': 0.0}


# -- the thread at work ---------------------------------------------------------------------------------


@pytest.mark.usefixtures('fast')
def test_a_cache_nobody_touches_is_swept(tmp_path: Path) -> None:
    with Cache(tmp_path / 'c', maintenance_interval=FAST) as cache:
        expiring(cache, 300)
        cache_db = tmp_path / 'c' / DB
        assert eventually(lambda: stored_rows(cache_db) == 0), 'the expired rows were never removed'
        assert eventually(lambda: cache.maintenance_stats()['removed'] == 300), cache.maintenance_stats()
        stats = cache.maintenance_stats()
        assert stats['turns'] > 0 and stats['failures'] == 0, stats


@pytest.mark.usefixtures('fast')
def test_every_shard_of_a_fanout_cache_is_swept(tmp_path: Path) -> None:
    with FanoutCache(tmp_path / 'f', shards=4, maintenance_interval=FAST) as cache:
        expiring(cache, 200)
        shards = [tmp_path / 'f' / f'{i:03d}' / DB for i in range(4)]
        assert eventually(lambda: stored_rows(*shards) == 0), 'a shard was never swept'


@pytest.mark.usefixtures('fast')
def test_a_cache_with_its_own_clock_is_never_swept(tmp_path: Path) -> None:
    now = [time.time()]
    with Cache(tmp_path / 'c', clock=lambda: now[0]) as clocked, Cache(tmp_path / 'c') as other:
        clocked.set('k', 'v', expire=0.05)
        # Expired by the wall clock, live by the cache's; the other handle asked for the default.
        with Cache(tmp_path / 'busy', maintenance_interval=FAST) as busy:
            expiring(busy, 1, expire=0.05)
            assert eventually(lambda: stored_rows(tmp_path / 'busy' / DB) == 0), 'the thread never ran'
            time.sleep(4 * FAST)
            assert clocked.get('k') == 'v'
            assert stored_rows(tmp_path / 'c' / DB) == 1
            assert other.maintenance_stats()['interval'] == 0.0


@pytest.mark.usefixtures('fast')
@pytest.mark.parametrize('call', ['get', 'incr'])
def test_a_failure_is_counted_and_logged_once(tmp_path: Path, caplog: pytest.LogCaptureFixture, call: str) -> None:
    with Cache(tmp_path / 'c', maintenance_interval=FAST) as cache:
        expiring(cache, 5)
        _core._fail_next_maintenance_turns(3)
        assert eventually(lambda: cache.maintenance_stats()['failures'] >= 1)
        with caplog.at_level(logging.WARNING, logger='diskcache_oxide'):
            for _ in range(2):
                cache.get('anything') if call == 'get' else cache.incr('n')
        logged = [r for r in caplog.records if 'background maintenance' in r.getMessage()]
        assert len(logged) == 1, [r.getMessage() for r in caplog.records]
        assert 'injected' in logged[0].getMessage()
        assert cache.maintenance_stats()['last_error'] is not None
        # The three injected failures run out, and turns carry on.
        assert eventually(lambda: cache.maintenance_stats()['turns'] >= 4), cache.maintenance_stats()


# -- forks and shutdown -------------------------------------------------------------------------------


def run(script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, '-W', 'always', '-c', textwrap.dedent(script), *args],
        capture_output=True,
        text=True,
        timeout=60,
    )


FORK_WARNING = 'multi-threaded'


@pytest.mark.skipif(sys.platform == 'win32', reason='no fork() on Windows')
def test_the_fork_warning_this_test_looks_for_is_real() -> None:
    # The positive control for the test below: if a Python release rewords the warning, this fails
    # rather than letting that one pass for nothing.
    script = """
        import os, threading, time
        threading.Thread(target=time.sleep, args=(2,), daemon=True).start()
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
    """
    assert FORK_WARNING in run(script).stderr


@pytest.mark.skipif(sys.platform == 'win32', reason='no fork() on Windows')
def test_forking_with_maintenance_running_never_warns_and_both_sides_carry_on(tmp_path: Path) -> None:
    script = """
        import os, sys, time
        from diskcache_oxide import Cache, _core
        _core._set_maintenance_floor(0.01)
        cache = Cache(sys.argv[1], maintenance_interval=0.05)

        def turns_reach(n):
            deadline = time.monotonic() + 10
            while cache.maintenance_stats()['turns'] < n and time.monotonic() < deadline:
                time.sleep(0.01)
            return cache.maintenance_stats()['turns'] >= n

        assert turns_reach(3), 'the thread never ran before the fork'
        before = cache.maintenance_stats()['turns']
        pid = os.fork()
        if pid == 0:
            # The parent's counts don't come across, and the child's own thread runs.
            fresh = cache.maintenance_stats()['turns'] < before
            os._exit(0 if fresh and turns_reach(3) else 3)
        _, status = os.waitpid(pid, 0)
        print('child', os.waitstatus_to_exitcode(status))
        print('parent', turns_reach(before + 3))
        cache.close()
    """
    done = run(script, str(tmp_path / 'c'))
    assert done.returncode == 0, done.stderr
    assert 'child 0' in done.stdout and 'parent True' in done.stdout, (done.stdout, done.stderr)
    assert FORK_WARNING not in done.stderr, done.stderr


def test_the_interpreter_exits_cleanly_mid_turn(tmp_path: Path) -> None:
    script = """
        import sys, time
        from diskcache_oxide import Cache, _core
        _core._set_maintenance_floor(0.01)
        cache = Cache(sys.argv[1], maintenance_interval=0.01)
        with cache.transact():
            for i in range(20_000):
                cache.set(f'k{i}', i, expire=0.01)
        time.sleep(0.05)
        # Exits with turns in progress and the cache never closed.
    """
    started = time.monotonic()
    done = run(script, str(tmp_path / 'c'))
    assert (done.returncode, done.stderr) == (0, '')
    assert time.monotonic() - started < 30


@pytest.mark.skipif(not interval_from_env(), reason='only under --maintenance-interval')
def test_the_mode_reaches_the_processes_tests_start(tmp_path: Path) -> None:
    # As fork_scenarios, fork_timeout_scenarios and proc_workers start: a fresh interpreter that
    # applies the run's interval from the environment.
    script = """
        import sys
        from diskcache_oxide import Cache
        from tests.maintenance import apply_from_env
        apply_from_env()
        with Cache(sys.argv[1]) as cache:
            print(cache.maintenance_stats()['interval'])
    """
    done = subprocess.run(
        [sys.executable, '-c', textwrap.dedent(script), str(tmp_path / 'c')],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=Path(__file__).resolve().parents[2],
    )
    assert done.returncode == 0, done.stderr
    assert float(done.stdout) == interval_from_env()
