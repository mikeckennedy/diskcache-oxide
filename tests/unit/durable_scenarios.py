"""`Cache(durable=True)` where the failure needs a process of its own (plan 09, step 2; D-028). Run from
test_durable.py.

    python -m tests.unit.durable_scenarios <scenario> <directory>

A stuck fork is the deterministic way to make `get` and `in` fail, and it stays stuck on purpose, so the
gate can't be shared. `RLIMIT_FSIZE` and a fork inside a hold are process-wide too. Exits 0 if every
check passed; otherwise prints the failures to stderr and exits 1. A hang ends the process through
`faulthandler`, with every thread's stack on stderr.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import signal
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

from diskcache_oxide import Cache, NullCache, Timeout, _core

from tests.unit import fork_timeout_scenarios
from tests.unit.fork_timeout_scenarios import (
    BIG_VALUE,
    SMALL_VALUE,
    TIMEOUT,
    WATCHDOG,
    Hooked,
    StuckFork,
    filled,
    on_thread,
    raises_timeout,
    run_hook,
    stats,
)

LOGGER = 'diskcache_oxide'
# How far past its timeout a durable call may return on a loaded machine.
SLACK = 1.3


class Captured(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


def capture() -> Captured:
    handler = Captured()
    logger = logging.getLogger(LOGGER)
    logger.addHandler(handler)
    logger.propagate = False
    return handler


def answers(what: str, work: Callable[[], Any], expected: Any) -> None:
    """On another thread, behind the fork: `work()` gives `expected` within its timeout, or more."""
    result, took = on_thread(work)
    assert result == expected and type(result) is type(expected), f'{what}: expected {expected!r}, got {result!r}'
    assert took >= TIMEOUT * 0.95, f'{what}: answered after only {took:.3f} s, so it never failed'
    assert took < TIMEOUT + SLACK, f'{what}: answered after {took:.3f} s'


def raises_key_error(what: str, work: Callable[[], Any]) -> None:
    result, took = on_thread(work)
    assert isinstance(result, KeyError), f'{what}: expected KeyError, got {result!r}'
    assert took < TIMEOUT + SLACK, f'{what}: raised after {took:.3f} s'


def every_row_behind_a_fork(directory: Path) -> None:
    """Each row of the table behind a stuck fork, which is the only way to fail `get` and `in` for
    sure. The first log line comes while the fork is stuck, so it has to be able to."""
    log = capture()
    c = filled(directory, 'c', durable=True)
    closed = filled(directory, 'closed', durable=True)
    closed.close()

    def set_item() -> None:
        c['k'] = 2

    def del_item() -> None:
        del c['del-me']

    before = stats()['fork_timeouts']
    with StuckFork(directory):
        answers('get', lambda: c.get('k', 'default'), 'default')
        answers('get None', lambda: c.get('k'), None)
        answers(
            'get(expire_time=True)',
            lambda: c.get('k', 'default', expire_time=True),
            ('default', None),
        )
        raises_key_error('[]', lambda: c['k'])
        answers('in', lambda: 'k' in c, False)
        answers('set small', lambda: c.set('k', SMALL_VALUE), False)
        answers('set spilled', lambda: c.set('k', BIG_VALUE), False)
        answers('[]=', set_item, None)
        answers('add', lambda: c.add('new', 1), False)
        answers('touch', lambda: c.touch('k', 60), False)
        answers('delete', lambda: c.delete('delete-me'), False)
        raises_key_error('del', del_item)
        answers('pop', lambda: c.pop('pop-me', 'default'), 'default')
        answers('incr', lambda: c.incr('n', 2, 5), 7)
        answers('decr', lambda: c.decr('n', 2, 5), 3)
        raises_key_error('incr default=None', lambda: c.incr('n', 1, None))
        # A closed handle can't rejoin behind the fork: it goes through the same seam.
        answers('get on a closed handle', lambda: closed.get('k', 'default'), 'default')
        answers('in on a closed handle', lambda: 'k' in closed, False)
        answers('set on a closed handle', lambda: closed.set('k', 2), False)
        answers('delete on a closed handle', lambda: closed.delete('k'), False)
        assert closed._handle_count() == 0, 'a failed reopen counted a handle'
        # Logged while the fork is still stuck.
        assert len(log.records) >= 2, [r.getMessage() for r in log.records]
    # Nothing was changed, and the handle works again.
    assert c.get('k') == 1
    assert c.get('pop-me') == 1
    assert c.get('delete-me') == 1
    assert c.get('del-me') == 1
    assert c.get('n') == 1
    assert c.get('new') is None
    assert closed.get('k') == 1
    assert stats()['fork_timeouts'] > before
    busy = c._durable_stats()['busy']
    assert busy['get'] == 3 + 1, busy
    assert busy['in'] == 1
    assert busy['set'] == 3
    assert busy['pop'] == 1
    assert (busy['incr'], busy['decr']) == (2, 1)
    assert c._durable_stats()['lost_deletes']['delete'] == 2
    levels = {r.levelno for r in log.records}
    assert levels == {logging.WARNING, logging.ERROR}, levels
    c.close()
    closed.close()


def default_raises_behind_a_fork(directory: Path) -> None:
    """The same handle shape without `durable`: every single-key call raises `Timeout`."""
    c = filled(directory, 'c')
    with StuckFork(directory):
        raises_timeout('get', lambda: c.get('k'))
        raises_timeout('in', lambda: 'k' in c)
        raises_timeout('set', lambda: c.set('k', 2))
        raises_timeout('add', lambda: c.add('k', 2))
        raises_timeout('touch', lambda: c.touch('k'))
        raises_timeout('delete', lambda: c.delete('k'))
        raises_timeout('pop', lambda: c.pop('k'))
        raises_timeout('incr', lambda: c.incr('n'))
    c.close()


def admin_raises_behind_a_fork(directory: Path) -> None:
    """Admin callers need the truth, and these have their own `timeout=`."""
    c = filled(directory, 'c', durable=True)
    fresh = directory / 'fresh'
    with StuckFork(directory):
        raises_timeout('len', lambda: len(c))
        raises_timeout('volume', lambda: c.volume())
        raises_timeout('iter', lambda: next(iter(c)))
        raises_timeout('iterkeys', lambda: next(c.iterkeys()))
        raises_timeout('clear', lambda: c.clear(timeout=TIMEOUT))
        raises_timeout('expire', lambda: c.expire(timeout=TIMEOUT))
        raises_timeout('cull', lambda: c.cull(timeout=TIMEOUT))
        raises_timeout('Cache(...)', lambda: Cache(fresh, timeout=TIMEOUT, durable=True))
        raises_timeout('a hold starting', lambda: c.transact().__enter__())
        result, _ = on_thread(lambda: c.checkpoint(timeout=TIMEOUT))
        assert result is False, f'checkpoint: {result!r}'
    assert sum(c._durable_stats()['busy'].values()) == 0, 'none of those counts as swallowed'
    c.close()


def a_held_transaction_never_times_out_behind_its_own_fork(directory: Path) -> None:
    """Inside the hold the calls pass the fork, so they run: there's no failure to swallow. A
    durable handle answers them as usual."""
    held = Cache(directory / 'held', timeout=TIMEOUT, durable=True)
    held.set('k', 1)
    fork = StuckFork(directory)
    fork.held = held
    with fork:
        assert held.get('k') == 1
        assert 'k' in held
        assert held.set('k', 2) is True
        assert held.pop('k') == 2
    assert held.get('k') is None
    held.close()


def hook_reraising_clears_the_cycle(directory: Path) -> None:
    """A hook re-raises the `Timeout` it got from another thread's call while a fork waits for its
    level. A durable `get` makes that a miss with the row kept, the level goes, and the fork ends, with
    no more code from the caller."""
    main = Cache(directory / 'main', timeout=30.0, pickle=True, durable=True)
    other = filled(directory, 'other')
    main.set('k', Hooked())
    fork = StuckFork(directory)

    def body() -> None:
        result, _ = on_thread(lambda: other.get('k'))
        assert isinstance(result, Timeout), result
        raise result

    fork_timeout_scenarios.HOOK = run_hook(fork, body)
    assert main.get('k', 'default') == 'default'
    fork.finished()
    assert isinstance(main.get('k'), Hooked), 'the row stayed'
    main.close()
    other.close()


def rlimit_fsize(directory: Path) -> None:
    """A real I/O failure: `RLIMIT_FSIZE` in a forked child, with `SIGXFSZ` ignored, so a write past the
    limit is `EFBIG`. A spilled value fails on its file, and a small one on the WAL."""
    import resource

    c = Cache(directory / 'c', durable=True)
    plain = Cache(directory / 'c')
    c.set('k', 'old')
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            log = capture()
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
            _, hard = resource.getrlimit(resource.RLIMIT_FSIZE)
            resource.setrlimit(resource.RLIMIT_FSIZE, (1, hard))
            assert c.set('big', BIG_VALUE) is False, 'a value file past the limit'
            assert not list((directory / 'c').rglob('*.val')), 'a dropped set left its value file'
            try:
                plain.set('big', BIG_VALUE)
            except OSError as exc:
                assert 'too large' in str(exc).lower(), exc
            else:
                raise AssertionError('the default raises')
            assert c.add('big', BIG_VALUE) is False, 'a value file past the limit'
            assert not list((directory / 'c').rglob('*.tmp')), 'a failed write left its partial file'
            assert c.set('small', 'y' * 2000) is False, 'the WAL past the limit'
            assert c.add('small', 'y' * 2000) is False
            assert c.incr('n', 1, 5) == 6
            assert c.delete('k') is False
            assert c.pop('k', 'default') == 'default'
            # Opening a connection here can need the WAL index, so a read may fail too. It doesn't raise.
            assert c.get('k', 'default') in ('old', 'default')
            stats_now = c._durable_stats()
            assert stats_now['broken']['set'] == 2, stats_now
            assert stats_now['broken']['pop'] == 1, stats_now
            assert stats_now['lost_deletes']['delete'] == 1, stats_now
            assert {r.levelno for r in log.records} == {logging.ERROR}, log.records
            # Past the failure the handle works again.
            resource.setrlimit(resource.RLIMIT_FSIZE, (hard, hard))
            assert c.get('k') == 'old'
            assert c.set('after', 1) is True
            assert c.get('after') == 1
            code = 0
        except BaseException:
            traceback.print_exc()
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    assert os.waitstatus_to_exitcode(status) == 0, 'the child failed'
    assert c.get('k') == 'old'
    assert c.get('after') == 1
    c.close()
    plain.close()


def fork_inside_a_hold(directory: Path) -> None:
    """A child forked inside a transaction can't write that database (D-020), and can't use the
    transaction: both are `RuntimeError`s on a plain handle and a miss or False on a durable one."""
    import warnings

    c = Cache(directory / 'c', durable=True)
    plain = Cache(directory / 'c')
    c.set('k', 'old')
    c.set('n', 10)
    c.set('in-hold', 'old')
    hold = c.transact()
    hold.__enter__()
    c.set('in-hold', 'new')
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        pid = os.fork()
    if pid == 0:
        code = 1
        try:
            # This thread's inherited transaction: every call on it is refused.
            assert c.get('k', 'default') == 'default', 'the inherited transaction'
            assert 'k' not in c
            assert c.set('k', 'child') is False
            assert c.add('new', 1) is False
            assert c.touch('k') is False
            assert c.delete('k') is False
            assert c.pop('k', 'default') == 'default'
            assert c.incr('n', 1, 5) == 6
            try:
                plain.set('k', 'child')
            except RuntimeError:
                pass
            else:
                raise AssertionError('the default raises')
            try:
                plain.get('k')
            except RuntimeError:
                pass
            else:
                raise AssertionError('the default raises')
            stats_now = c._durable_stats()
            assert stats_now['broken']['get'] == 1, stats_now
            assert stats_now['broken']['set'] == 1, stats_now
            assert stats_now['lost_deletes']['delete'] == 1, stats_now
            code = 0
        except BaseException:
            traceback.print_exc()
        finally:
            os._exit(code)
    _, status = os.waitpid(pid, 0)
    hold.__exit__(None, None, None)
    assert os.waitstatus_to_exitcode(status) == 0, 'the child failed'
    assert c.get('k') == 'old', 'the child changed nothing'
    assert c.get('in-hold') == 'new', 'the parent committed'
    c.close()
    plain.close()


def durable_stays_out_of_null_cache(directory: Path) -> None:
    """A `Caches` lookup behind a stuck fork gets a `NullCache`, whose `add` is True where a durable
    `Cache`'s is False. That's the documented reason to keep claims on a handle that isn't durable."""
    from diskcache_oxide import Caches

    caches: Caches = Caches(directory / 'base', timeout=TIMEOUT, durable=True)
    caches.register('c')
    durable = filled(directory, 'open', durable=True)
    with StuckFork(directory):
        null, _ = on_thread(lambda: caches['c'])
        assert isinstance(null, NullCache), null
        assert null.add('k', 1) is True
        answers('a durable add', lambda: durable.add('claim', 1), False)
    durable.close()
    caches.close_all()


SCENARIOS: dict[str, Callable[[Path], None]] = {
    'every-row-behind-a-fork': every_row_behind_a_fork,
    'default-raises-behind-a-fork': default_raises_behind_a_fork,
    'admin-raises-behind-a-fork': admin_raises_behind_a_fork,
    'hold-behind-its-own-fork': a_held_transaction_never_times_out_behind_its_own_fork,
    'hook-reraising-clears-the-cycle': hook_reraising_clears_the_cycle,
    'rlimit-fsize': rlimit_fsize,
    'fork-inside-a-hold': fork_inside_a_hold,
    'add-on-a-null-cache': durable_stays_out_of_null_cache,
}


if __name__ == '__main__':
    faulthandler.dump_traceback_later(WATCHDOG, exit=True)
    outcome = 0
    try:
        assert _core is not None
        SCENARIOS[sys.argv[1]](Path(sys.argv[2]))
    except BaseException:
        traceback.print_exc()
        outcome = 1
    sys.stdout.flush()
    sys.stderr.flush()
    # Not a normal exit: on a failure, a thread may still be stuck behind the fork.
    os._exit(outcome)
