"""A pending fork can't outlast `timeout` (plan 09, step 1b; D-027). Run from test_fork_timeout.py.

    python -m tests.unit.fork_timeout_scenarios <scenario> <directory>

Each scenario runs in a fresh process, since the gate is process-wide and a fork stays stuck on
purpose. Exits 0 if every check passed; otherwise prints the failures to stderr and exits 1. A hang
ends the process through `faulthandler`, with every thread's stack on stderr.
"""

from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

WATCHDOG = 40.0
# A call's timeout behind a fork, and how far past it a call may return on a loaded machine.
TIMEOUT = 0.2
SLACK = 1.3
# `close()` behind a stuck fork: 1 s for the release, and 1 s more for the checkpoint on a large WAL.
RELEASE = 1.0
CALL_LIMIT = 10.0
# Worst interpreter stall a ticker may see while a caller waits behind a fork. A wait that stays
# attached stalls it for the whole 0.5 s timeout (501 ms measured), and a correct wait about 1 ms.
MAX_TICK_GAP = 0.15
SMALL_VALUE = 'v'
BIG_VALUE = b'x' * (64 * 1024)


def stats() -> dict[str, Any]:
    from diskcache_oxide import _core

    return _core._gate_stats()


def wait_until(condition: Callable[[], bool], what: str, limit: float = 10.0) -> None:
    end = time.monotonic() + limit
    while not condition():
        assert time.monotonic() < end, f'{what}: never happened'
        time.sleep(0.001)


def on_thread(work: Callable[[], Any], limit: float = CALL_LIMIT) -> tuple[Any, float]:
    """`work()` on a new thread: its result, or the exception it raised, and how long it took."""
    box: list[tuple[Any, float]] = []

    def run() -> None:
        start = time.monotonic()
        try:
            result: Any = work()
        except BaseException as exc:
            result = exc
        box.append((result, time.monotonic() - start))

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(limit)
    assert box, f'{work}: no return in {limit} s'
    return box[0]


def raises_timeout(what: str, work: Callable[[], Any], timeout: float = TIMEOUT) -> None:
    from diskcache_oxide import Timeout

    result, took = on_thread(work)
    assert isinstance(result, Timeout), f'{what}: expected Timeout, got {result!r}'
    assert took >= timeout * 0.95, f'{what}: Timeout after only {took:.3f} s'
    assert took < timeout + SLACK, f'{what}: Timeout after {took:.3f} s'


class StuckFork:
    """A transaction held on this thread, with another thread in `os.fork()` waiting for it.

    The child reads and writes through `child`, if given. On exit the transaction commits, and the fork
    must complete and its child exit cleanly.
    """

    def __init__(self, directory: Path, child: Callable[[], None] | None = None) -> None:
        from diskcache_oxide import Cache

        self.held = Cache(directory / 'held', timeout=30.0)
        self.child = child
        self.exit_code: int | None = None
        self.done = threading.Event()

    def _fork(self) -> None:
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                if self.child is not None:
                    self.child()
            except BaseException:
                traceback.print_exc()
                code = 1
            finally:
                os._exit(code)
        _, status = os.waitpid(pid, 0)
        self.exit_code = os.waitstatus_to_exitcode(status)
        self.done.set()

    def start_fork(self) -> None:
        threading.Thread(target=self._fork, daemon=True).start()
        wait_until(lambda: stats()['pending'], 'the fork claiming')

    def __enter__(self) -> StuckFork:
        self.hold = self.held.transact()
        self.hold.__enter__()
        self.start_fork()
        return self

    def __exit__(self, *exc: object) -> None:
        self.hold.__exit__(None, None, None)
        self.finished()

    def finished(self) -> None:
        assert self.done.wait(CALL_LIMIT), 'the fork never completed'
        assert self.exit_code == 0, f'the child exited with {self.exit_code}'
        wait_until(lambda: stats()['holders'] == 0, 'the holders draining')
        current = stats()
        assert current['parked'] == 0, current
        assert not current['pending'], current


def filled(directory: Path, name: str, timeout: float = TIMEOUT, **settings: Any) -> Any:
    from diskcache_oxide import Cache

    with Cache(directory / name, timeout=30.0) as setup:
        for key in ('k', 'pop-me', 'delete-me', 'del-me', 'n'):
            setup.set(key, 1)
    return Cache(directory / name, timeout=timeout, **settings)


# -- every public call --------------------------------------------------------------------------


def calls_on(c: Any, timeout: float) -> dict[str, Callable[[], Any]]:
    def del_item() -> None:
        del c['del-me']

    def set_item() -> None:
        c['k'] = 2

    return {
        'get': lambda: c.get('k'),
        'get(expire_time=True)': lambda: c.get('k', expire_time=True),
        '[]': lambda: c['k'],
        'in': lambda: 'k' in c,
        'len': lambda: len(c),
        'volume': lambda: c.volume(),
        'set small': lambda: c.set('new', SMALL_VALUE),
        'set spilled': lambda: c.set('new-big', BIG_VALUE),
        '[]=': set_item,
        'add': lambda: c.add('new-add', SMALL_VALUE),
        'delete': lambda: c.delete('delete-me'),
        'del': del_item,
        'pop': lambda: c.pop('pop-me'),
        'touch': lambda: c.touch('k', 100),
        'incr': lambda: c.incr('n'),
        'decr': lambda: c.decr('n'),
        'next(iterkeys())': lambda: next(c.iterkeys()),
        'next(iter())': lambda: next(iter(c)),
        'expire': lambda: c.expire(timeout=timeout),
        'clear': lambda: c.clear(timeout=timeout),
        'cull': lambda: c.cull(timeout=timeout),
    }


def every_public_call(directory: Path) -> None:
    """Each call on another thread raises `Timeout` within the bound, and nothing is lost."""
    from diskcache_oxide import Cache

    c = filled(directory, 'calls')
    closed = filled(directory, 'closed')
    closed.close()
    existing = filled(directory, 'open-already')
    fork_timeouts = stats()['fork_timeouts']
    raised = 0

    def child() -> None:
        assert c.get('k') == 1, 'the child reads what the parent wrote'
        assert c.set('from-child', 1)
        assert c.get('from-child') == 1

    with StuckFork(directory, child) as fork:
        for what, call in calls_on(c, TIMEOUT).items():
            raises_timeout(what, call)
            raised += 1
        raises_timeout('Cache(new)', lambda: Cache(directory / 'new', timeout=TIMEOUT))
        raises_timeout('Cache(open)', lambda: Cache(directory / 'open-already', timeout=TIMEOUT))
        raises_timeout(
            'Cache(open, size_limit)',
            lambda: Cache(directory / 'open-already', timeout=TIMEOUT, size_limit=2**24),
        )
        raised += 3
        raises_timeout('a closed handle reused', lambda: closed.get('k'))
        raised += 1
        raises_timeout('transact', lambda: c.transact().__enter__())
        raised += 1
        assert existing._handle_count() == 1, 'a timed-out open claimed a handle'
        assert closed._handle_count() == 0, 'a timed-out reopen counted a handle'
        before = c._wal_stats()
        assert on_thread(lambda: c.checkpoint())[0] is False
        after = c._wal_stats()
        assert after['fork_pending'] == before['fork_pending'] + 1, after
        assert after['busy'] == before['busy'], after
        assert on_thread(lambda: closed.checkpoint())[0] is False, 'a closed handle raised for a busy database'
        raised += 1
        assert closed._handle_count() == 0, 'a timed-out checkpoint counted a handle'
        current = stats()
        assert current['pending'] and current['fork_timeouts'] - fork_timeouts == raised, (current, raised)
        assert not fork.done.is_set(), 'the fork ended while the transaction was held'
    assert stats()['fork_timeouts'] - fork_timeouts == raised

    assert closed.get('k') == 1, 'the closed handle reopens once the fork has ended'
    assert closed._handle_count() == 1
    assert c.get('from-child') == 1, "the child's write survived"
    for key in ('k', 'pop-me', 'delete-me', 'del-me', 'n'):
        assert c.get(key) == 1, f'{key} changed behind the fork'
    for key in ('new', 'new-big', 'new-add'):
        assert key not in c, f'{key} was written behind the fork'
    value_files = [
        p for p in (directory / 'calls').rglob('*') if p.is_file() and not p.name.startswith('diskcache-oxide')
    ]
    assert not value_files, f'a timed-out spilled write left {value_files}'


def timeout_zero(directory: Path) -> None:
    """`timeout=0`: `Timeout` at once, with no wait."""
    from diskcache_oxide import Timeout

    c = filled(directory, 'zero', timeout=0)
    with StuckFork(directory):
        for what, call in calls_on(c, 0).items():
            result, took = on_thread(call)
            assert isinstance(result, Timeout), f'{what}: {result!r}'
            assert took < SLACK, f'{what}: waited {took:.3f} s'


def nested_calls_never_time_out(directory: Path) -> None:
    """A thread holding a level walks past the fork, on any cache."""
    c = filled(directory, 'nested')
    fork_timeouts = stats()['fork_timeouts']
    with StuckFork(directory) as fork:
        assert fork.held.set('in-hold', 1)
        assert fork.held.get('in-hold') == 1
        assert c.get('k') == 1
        assert c.set('nested', 1)
        assert c.pop('pop-me') == 1
        assert stats()['pending']
    assert stats()['fork_timeouts'] == fork_timeouts
    assert fork.held.get('in-hold') == 1, 'the hold committed'
    assert c.get('nested') == 1


def iterator_resumes(directory: Path) -> None:
    """An iterator that times out carries on from the right key once the fork has ended."""
    c = filled(directory, 'iter')
    it = c.iterkeys()
    first = sorted(['k', 'pop-me', 'delete-me', 'del-me', 'n'])
    assert [next(it) for _ in first] == first
    c.set('z', 1)
    with StuckFork(directory):
        raises_timeout('next on a live iterator', lambda: next(it))
    assert next(it) == 'z', 'it resumed at the wrong key'
    assert next(it, None) is None


# -- close and del ------------------------------------------------------------------------------


def fds_inside(directory: Path) -> int:
    """How many of this process's open fds point at a path inside `directory`."""
    root = os.path.realpath(directory) + os.sep

    def path_of(fd: int) -> str | None:
        try:
            if sys.platform == 'darwin':
                import fcntl

                return fcntl.fcntl(fd, fcntl.F_GETPATH, bytes(1024)).rstrip(b'\0').decode()
            return os.readlink(f'/proc/self/fd/{fd}')
        except OSError:
            return None

    fd_dir = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    paths = (path_of(int(name)) for name in os.listdir(fd_dir))
    return sum(1 for path in paths if path is not None and os.path.realpath(path).startswith(root))


def swollen(directory: Path, timeout: float) -> Any:
    """A cache whose WAL is past its trim size, left by a burst under a peer's open snapshot."""
    from diskcache_oxide import Cache

    from tests.conformance.proc_workers import SqlitePeer

    limit = 1 << 20
    cache = Cache(directory, wal_size_limit=limit, timeout=timeout)
    cache.set('k', 1)
    with SqlitePeer(directory / 'diskcache-oxide.sqlite') as peer:
        peer.begin('READ')
        for i in range(200):
            cache.set(f'b{i % 50}', 'x' * 20_000)
        peer.rollback()
    assert (directory / 'diskcache-oxide.sqlite-wal').stat().st_size > limit // 2
    return cache


class DroppedOnItsOwnThread:
    """A cache made and dropped on one thread. On a free-threaded build a reference dropped by a thread
    that doesn't own the object can leave the deallocation to its owner, so a `del` elsewhere may not
    run `Drop` at all by the time it returns."""

    def __init__(self, make: Callable[[], Any]) -> None:
        self.ready = threading.Event()
        self.go = threading.Event()
        self.took: float | None = None
        self.thread = threading.Thread(target=self._run, args=(make,), daemon=True)
        self.thread.start()
        assert self.ready.wait(CALL_LIMIT), 'the cache never opened'

    def _run(self, make: Callable[[], Any]) -> None:
        cache = make()
        assert cache.get('k') == 1
        self.ready.set()
        self.go.wait()
        start = time.monotonic()
        del cache
        self.took = time.monotonic() - start

    def start_del(self) -> None:
        self.go.set()

    def del_and_time(self) -> float:
        self.start_del()
        self.thread.join(CALL_LIMIT)
        assert self.took is not None, 'del never returned'
        return self.took


def close_defers(directory: Path) -> None:
    """`close()` and `del` return within their budgets, whatever `timeout` is, and defer. D-018 holds
    once the fork has ended and its after-hooks have drained them."""
    from diskcache_oxide import Cache

    small = filled(directory, 'small', timeout=30.0)
    dropped = DroppedOnItsOwnThread(lambda: filled(directory, 'dropped', timeout=0))
    assert small.get('k') == 1
    fork_timeouts = stats()['fork_timeouts']

    def child() -> None:
        # The child inherited the count, and its own drain forgot the connections.
        assert small._handle_count() == 0
        assert not stats()['deferred']
        assert Cache(directory / 'small', timeout=5.0).get('k') == 1

    with StuckFork(directory, child):
        _, took = on_thread(lambda: small.close())
        assert RELEASE * 0.95 <= took < RELEASE + SLACK, f'close() took {took:.3f} s'
        assert stats()['deferred'], 'the release should be deferred'
        assert small._handle_count() == 1, 'it is still counted until the drain'
        assert fds_inside(directory / 'small') > 0, 'the connections wait for the drain'

        took = dropped.del_and_time()
        assert RELEASE * 0.95 <= took < RELEASE + SLACK, f'del took {took:.3f} s'
        assert stats()['fork_timeouts'] - fork_timeouts == 2
    wait_until(lambda: not stats()['deferred'], 'the drain')
    assert small._handle_count() == 0
    assert fds_inside(directory / 'small') == 0, 'D-018: a connection stayed open after the drain'
    assert fds_inside(directory / 'dropped') == 0, 'D-018: del left a connection open'
    assert small.get('k') == 1, 'the closed handle reopens'
    assert small._handle_count() == 1


def close_large_wal(directory: Path) -> None:
    """The detached path: 1 s for the checkpoint and 1 s for the release, through `crate::detach`."""
    a = swollen(directory / 'a', 30.0)
    b = DroppedOnItsOwnThread(lambda: swollen(directory / 'b', 30.0))
    with StuckFork(directory):
        _, took = on_thread(lambda: a.close())
        assert 2 * RELEASE * 0.95 <= took < 2 * RELEASE + SLACK, f'close() took {took:.3f} s'
        assert a._wal_stats()['fork_pending'] == 1, a._wal_stats()

        # Drop has no checkpoint: the release alone, through `crate::detach` and `try_attach`.
        took = b.del_and_time()
        assert RELEASE * 0.95 <= took < RELEASE + SLACK, f'del took {took:.3f} s'
    wait_until(lambda: not stats()['deferred'], 'the drain')
    assert fds_inside(directory / 'a') == 0
    assert fds_inside(directory / 'b') == 0


def del_during_an_ordinary_fork(directory: Path) -> None:
    """At `timeout=0`, a `del` during an ordinary fork waits it out and defers nothing."""
    dropped = DroppedOnItsOwnThread(lambda: filled(directory, 'zero', timeout=0))
    fork_timeouts = stats()['fork_timeouts']
    fork = StuckFork(directory)
    fork.__enter__()
    dropped.start_del()
    time.sleep(0.05)
    fork.__exit__()
    dropped.thread.join(CALL_LIMIT)
    assert not dropped.thread.is_alive()
    assert not stats()['deferred'], 'an ordinary fork deferred a release'
    assert stats()['fork_timeouts'] == fork_timeouts
    assert fds_inside(directory / 'zero') == 0


def open_with_settings(directory: Path) -> None:
    """`Cache(dir, size_limit=...)` behind a fork: the one `timeout`, and no handle left counted. It
    times out at the lookup, so it never reaches `join`: the Rust test `row_5_a_failed_join...` covers
    `join`'s failure path."""
    from diskcache_oxide import Cache, Timeout

    c = filled(directory, 'join', timeout=30.0)
    with StuckFork(directory):
        result, took = on_thread(lambda: Cache(directory / 'join', timeout=TIMEOUT, size_limit=2**24))
        assert isinstance(result, Timeout), result
        assert took < TIMEOUT + SLACK * 0.7, f'took {took:.3f} s'
    assert c._handle_count() == 1, 'the failed open left a handle counted'


# -- the ticker ----------------------------------------------------------------------------------


@contextmanager
def ticking() -> Generator[list[float]]:
    gaps: list[float] = []
    stop = threading.Event()

    def tick() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.001)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    thread = threading.Thread(target=tick, daemon=True)
    thread.start()
    time.sleep(0.05)
    gaps.clear()
    yield gaps
    stop.set()
    thread.join(CALL_LIMIT)


def ticker_gap(directory: Path) -> None:
    """A wait behind a fork leaves the interpreter free, for attached and detached callers. "Raises
    within the bound" can't see this: an attached wait raises the same `Timeout`, on time."""
    from diskcache_oxide import Cache, Timeout

    assert sys._is_gil_enabled(), 'only a GIL build can see an attached wait this way'
    timeout = 0.5
    c = filled(directory, 'ticks', timeout=timeout)
    callers: dict[str, Callable[[], Any]] = {
        'get (attached)': lambda: c.get('k'),
        'set (attached)': lambda: c.set('k', 2),
        'Cache() (detached)': lambda: Cache(directory / 'new', timeout=timeout),
        'clear (detached)': lambda: c.clear(),
    }
    worst: dict[str, float] = {}
    with StuckFork(directory):
        for what, call in callers.items():
            with ticking() as gaps:
                result, took = on_thread(call)
                worst[what] = max(gaps, default=0.0)
            assert isinstance(result, Timeout), f'{what}: {result!r}'
            assert took >= timeout * 0.95, f'{what}: {took:.3f} s'
    print(
        f'worst ticker gaps: { {k: round(v * 1e3, 1) for k, v in worst.items()} } ms',
        file=sys.stderr,
    )
    for what, gap in worst.items():
        assert gap < MAX_TICK_GAP, f'{what}: the interpreter stalled for {gap * 1e3:.0f} ms during the wait'


# -- pickle hooks ----------------------------------------------------------------------------------

# What the hook does. Set by each scenario before its `get`.
HOOK: Callable[[], None] | None = None


def rebuild() -> Hooked:
    hook = HOOK
    if hook is not None:
        hook()
    return Hooked()


class Hooked:
    """Unpickling it runs `HOOK`, holding a gate level when the value is inline."""

    def __reduce__(self) -> tuple[Any, ...]:
        return (rebuild, ())


def hook_scene(directory: Path) -> tuple[Any, Any, Any]:
    from diskcache_oxide import Cache

    main = Cache(directory / 'main', timeout=30.0, pickle=True)
    other = filled(directory, 'other')
    main.set('k', Hooked())
    fork = StuckFork(directory)
    return main, other, fork


def run_hook(fork: Any, body: Callable[[], None]) -> Callable[[], None]:
    """A one-shot `HOOK`: starts the fork, which waits for this hook's level, then runs `body`."""

    def hook() -> None:
        global HOOK
        HOOK = None
        fork.start_fork()
        time.sleep(0.02)
        assert stats()['pending']
        body()
        assert not fork.done.is_set(), 'the fork ended while the hook held its level'

    return hook


def hook_waits_for(directory: Path) -> None:
    """A hook waits for another thread's get, set, close() and Cache(...): each returns as the table
    says, the hook's level goes, and the fork completes."""
    from diskcache_oxide import Cache

    global HOOK
    main, other, fork = hook_scene(directory)

    def body() -> None:
        raises_timeout('get', lambda: other.get('k'))
        raises_timeout('set', lambda: other.set('k', 2))
        raises_timeout('Cache(...)', lambda: Cache(directory / 'new', timeout=TIMEOUT))
        _, took = on_thread(lambda: other.close())
        assert took < RELEASE + SLACK, f'close() took {took:.3f} s'

    HOOK = run_hook(fork, body)
    assert isinstance(main.get('k'), Hooked)
    fork.finished()
    wait_until(lambda: not stats()['deferred'], 'the drain')
    assert fds_inside(directory / 'other') == 0


def hook_reraises(directory: Path) -> None:
    """The hook re-raises the `Timeout` it received: the caller's `get` raises it under 'raise' and
    'miss', with the row kept."""
    from diskcache_oxide import Cache, Timeout

    global HOOK
    for policy in ('raise', 'miss'):
        scene = directory / policy
        main = Cache(scene / 'main', timeout=30.0, pickle=True, on_decode_error=policy)
        other = filled(scene, 'other')
        main.set('k', Hooked())
        fork = StuckFork(scene)

        def body() -> None:
            result, _ = on_thread(lambda: other.get('k'))
            assert isinstance(result, Timeout), result
            raise result

        HOOK = run_hook(fork, body)
        try:
            main.get('k')
        except Timeout:
            pass
        else:
            raise AssertionError(f'{policy}: the Timeout did not reach the caller')
        fork.finished()
        assert isinstance(main.get('k'), Hooked), f'{policy}: the row was lost'


def hook_swallows(directory: Path) -> None:
    """The hook swallows the `Timeout` and returns: the caller gets its value."""
    from diskcache_oxide import Timeout

    global HOOK
    main, other, fork = hook_scene(directory)

    def body() -> None:
        result, _ = on_thread(lambda: other.get('k'))
        assert isinstance(result, Timeout), result

    HOOK = run_hook(fork, body)
    assert isinstance(main.get('k'), Hooked)
    fork.finished()


def hook_retries_until_stopped(directory: Path) -> None:
    """A hook that retries keeps the fork stuck, each call stays bounded, and it clears only when the
    hook stops: "clears only if the holder reacts"."""
    from diskcache_oxide import Timeout

    global HOOK
    main, other, fork = hook_scene(directory)

    def body() -> None:
        for _ in range(4):
            result, took = on_thread(lambda: other.get('k'))
            assert isinstance(result, Timeout), result
            assert took < TIMEOUT + SLACK, f'took {took:.3f} s'
            assert stats()['pending'] and not fork.done.is_set(), 'the fork should stay stuck'

    HOOK = run_hook(fork, body)
    assert isinstance(main.get('k'), Hooked)
    fork.finished()


def hook_on_a_spilled_value(directory: Path) -> None:
    """The control: a spilled value's hook holds no level, so there's no wedge."""
    global HOOK
    main, other, fork = hook_scene(directory)

    main.set('big', [Hooked(), 'x' * (64 * 1024)])
    seen: list[object] = []

    def hook() -> None:
        global HOOK
        HOOK = None
        # Not `start_fork`: with no level to wait for, the fork may be over before anyone sees it pending.
        threading.Thread(target=fork._fork, daemon=True).start()
        assert fork.done.wait(CALL_LIMIT), 'the fork waited for a hook that holds no level'
        seen.append(on_thread(lambda: other.get('k'))[0])

    HOOK = hook
    assert isinstance(main.get('big')[0], Hooked)
    assert seen == [1], seen
    fork.finished()


# -- other libraries' before-hooks ---------------------------------------------------------------


def hook_uses_logging_and_futures(directory: Path) -> None:
    """With the default import order, a hook can still submit work and log while a fork waits. This
    pins the hook order: run first, so `logging`'s and `concurrent.futures`' locks aren't held yet."""
    assert 'concurrent.futures.thread' not in sys.modules, 'the import order this pins'
    import diskcache_oxide  # noqa: F401, I001  (first, as an app does)
    from concurrent.futures import ThreadPoolExecutor
    import logging

    global HOOK
    main, other, fork = hook_scene(directory)

    def body() -> None:
        with ThreadPoolExecutor(1) as pool:
            result, took = on_thread(lambda: pool.submit(int).result())
            assert result == 0, result
            assert took < 2.0, f'submit took {took:.3f} s'
        result, took = on_thread(lambda: logging.getLogger('a.name.nobody.used.yet'))
        assert isinstance(result, logging.Logger), result
        assert took < 2.0, f'getLogger took {took:.3f} s'
        _, took = on_thread(lambda: logging.getLogger('diskcache_oxide').warning('from a hook behind a fork'))
        assert took < 2.0, f'the first warning took {took:.3f} s'

    HOOK = run_hook(fork, body)
    assert isinstance(main.get('k'), Hooked)
    fork.finished()


def futures_unimportable(directory: Path) -> None:
    """A failed import can't stop the hooks registering: a fork still quiesces."""
    sys.modules['concurrent.futures.thread'] = None  # ty: ignore[invalid-assignment]
    import diskcache_oxide
    from diskcache_oxide import _core

    assert diskcache_oxide.Cache
    assert _core._gate_stats()['pending'] is False
    other = filled(directory, 'other')
    with StuckFork(directory):
        raises_timeout('get', lambda: other.get('k'))


def caller_holding_the_logging_lock(directory: Path) -> None:
    """A thread calling the cache while it holds `logging`'s module lock, behind a fork: `Timeout` at
    its bound, and the fork then completes. The inversion the hook order costs, and why it's bounded."""
    import logging

    from diskcache_oxide import Timeout

    other = filled(directory, 'other')
    fork = StuckFork(directory)
    with logging._lock:  # ty: ignore[unresolved-attribute]
        fork.start_fork()
        start = time.monotonic()
        try:
            other.get('k')
        except Timeout:
            pass
        else:
            raise AssertionError('the get should time out behind the fork')
        took = time.monotonic() - start
        assert took < TIMEOUT + SLACK, f'took {took:.3f} s'
    fork.finished()


# -- the cross-thread route, and Caches ------------------------------------------------------------


def hold_waits_for_another_thread(directory: Path) -> None:
    """The reproduced wedge: a transaction waits for another thread's cache call, which waits for a
    fork, which waits for the transaction. That call now raises `Timeout`, and everything clears."""
    from diskcache_oxide import Timeout

    other = filled(directory, 'other')
    with StuckFork(directory) as fork:
        result, _ = on_thread(lambda: other.get('k'), limit=8.0)
        assert isinstance(result, Timeout), result
        assert not fork.done.is_set()
    assert other.get('k') == 1


def caches_first_lookup(directory: Path) -> None:
    """A first lookup behind a fork: a `NullCache`, a `WARNING` with no traceback, `on_open_error`,
    and the real cache after at most 1 s. A real failure keeps `retry_open_after` and `ERROR`."""
    import logging

    from diskcache_oxide import Cache, Caches, NullCache, Timeout

    records: list[logging.LogRecord] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logging.getLogger('diskcache_oxide').addHandler(Keep())

    class Clock:
        now = 1000.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    plain: Caches[str] = Caches(directory / 'plain', timeout=TIMEOUT, _monotonic=clock)
    plain.register('a')
    reports: list[tuple[str, Exception]] = []
    custom: Caches[str] = Caches(
        directory / 'custom',
        timeout=TIMEOUT,
        on_open_error=lambda name, exc: reports.append((name, exc)),
        _monotonic=clock,
    )
    custom.register('b')

    with StuckFork(directory):
        looked_up, _ = on_thread(lambda: plain['a'])
        assert isinstance(looked_up, NullCache), looked_up
        assert on_thread(lambda: plain['a'])[0] is looked_up, 'the failure is remembered'
        looked_up, _ = on_thread(lambda: custom['b'])
        assert isinstance(looked_up, NullCache), looked_up
    assert [r.levelno for r in records] == [logging.WARNING], records
    assert records[0].exc_info is None, 'a busy moment logs no traceback'
    assert len(reports) == 1 and isinstance(reports[0][1], Timeout), reports

    assert isinstance(plain['a'], NullCache), 'still remembered just before the second is up'
    clock.now += 1.01
    assert isinstance(plain['a'], Cache), 'the real cache opens after at most 1 s'
    assert isinstance(custom['b'], Cache)
    plain.close_all()
    custom.close_all()

    # `retry_open_after` under a second is kept.
    short: Caches[str] = Caches(directory / 'short', timeout=TIMEOUT, retry_open_after=0.25, _monotonic=clock)
    short.register('c')
    with StuckFork(directory):
        assert isinstance(on_thread(lambda: short['c'])[0], NullCache)
    clock.now += 0.26
    assert isinstance(short['c'], Cache)
    short.close_all()


SCENARIOS: dict[str, Callable[[Path], None]] = {
    'every-public-call': every_public_call,
    'timeout-zero': timeout_zero,
    'nested-calls': nested_calls_never_time_out,
    'iterator-resumes': iterator_resumes,
    'close-defers': close_defers,
    'close-large-wal': close_large_wal,
    'del-during-an-ordinary-fork': del_during_an_ordinary_fork,
    'open-with-settings': open_with_settings,
    'ticker-gap': ticker_gap,
    'hook-waits-for': hook_waits_for,
    'hook-reraises': hook_reraises,
    'hook-swallows': hook_swallows,
    'hook-retries': hook_retries_until_stopped,
    'hook-spilled': hook_on_a_spilled_value,
    'hook-logging-and-futures': hook_uses_logging_and_futures,
    'futures-unimportable': futures_unimportable,
    'logging-lock': caller_holding_the_logging_lock,
    'hold-waits-for-another-thread': hold_waits_for_another_thread,
    'caches-first-lookup': caches_first_lookup,
}


if __name__ == '__main__':
    faulthandler.dump_traceback_later(WATCHDOG, exit=True)
    outcome = 0
    try:
        SCENARIOS[sys.argv[1]](Path(sys.argv[2]))
    except BaseException:
        traceback.print_exc()
        outcome = 1
    sys.stdout.flush()
    sys.stderr.flush()
    # Not a normal exit: on a failure, a thread may still be stuck behind the fork.
    os._exit(outcome)
