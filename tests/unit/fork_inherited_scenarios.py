"""Fork scenarios for test_fork_inherited.py (plan 32): what a child can do with a cache, by what its
parent had open at the fork. Each runs in a fresh process (never fork pytest itself).

    python -m tests.unit.fork_inherited_scenarios <scenario> <directory>

SQLite keeps one lock record per database file per process, and a forked child inherits the record
but not the kernel locks behind it. A child that inherits an open connection therefore runs with no
locks: once the parent closes, its reads go stale and its writes are lost, with no error. So in every
scenario the parent closes its handle, and another process uses the cache, while the child lives:
the child must then either still work, with its writes there afterwards, or raise on every call.

The two hook lists below are registered before `diskcache_oxide` is imported, which is what orders
them against its own fork hooks: before-fork hooks run last-registered first, so `LATE_BEFORE` runs
after diskcache-oxide's, and `after_in_child` hooks run in registration order, so `EARLY_CHILD` runs
before it.
"""

from __future__ import annotations

import os
from collections.abc import Callable

LATE_BEFORE: list[Callable[[], None]] = []
EARLY_CHILD: list[Callable[[], None]] = []


def run_hooks(hooks: list[Callable[[], None]]) -> None:
    for hook in list(hooks):
        hook()


if hasattr(os, 'register_at_fork'):
    os.register_at_fork(before=lambda: run_hooks(LATE_BEFORE), after_in_child=lambda: run_hooks(EARLY_CHILD))

import subprocess  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402
import traceback  # noqa: E402
import warnings  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from diskcache_oxide import Cache, FanoutCache, _core, register_type, unregister_type  # noqa: E402

from tests.proctitle import set_title  # noqa: E402
from tests.unit.fork_scenarios import (  # noqa: E402
    TIMEOUT,
    ForkOnDecode,
    fails_fast,
    fork,
    fork_on_decode,
    one_key_per_shard,
    still_answers,
    unusable,
    wait,
)

Body = Callable[[], None]


def third_process(directory: Path, fanout: bool = False) -> None:
    """Another process opens the cache, writes `new`, and closes: with no kernel lock left on the
    database, its close checkpoints and deletes the WAL."""
    kind = 'FanoutCache' if fanout else 'Cache'
    script = f'from diskcache_oxide import {kind}; c = {kind}({str(directory)!r}); c.set("new", "visible"); c.close()'
    subprocess.run([sys.executable, '-c', script], check=True, timeout=TIMEOUT * 2)


def across_a_parent_close(
    directory: Path,
    fork_now: Callable[[Body], int],
    first: Body,
    second: Body,
    close: Body,
    fanout: bool = False,
) -> list[str]:
    """Forks with `fork_now(child)`. The child runs `first`; then the parent runs `close` and a
    third process uses the cache; then the child runs `second`. Returns what went wrong."""
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()

    def child() -> None:
        os.close(ready_r)
        os.close(go_w)
        first()
        os.write(ready_w, b'r')
        assert os.read(go_r, 1) == b'g', 'the parent never let the child go on'
        second()

    pid = fork_now(child)
    os.close(ready_w)
    os.close(go_r)
    failures = []
    if os.read(ready_r, 1) != b'r':
        failures.append("the child failed before the parent's close")
    close()
    third_process(directory, fanout)
    try:
        os.write(go_w, b'g')
    except BrokenPipeError:
        pass  # The child is gone already; its exit code says how.
    code = wait(pid)
    if code != 0:
        failures.append(f'child exit code {code}')
    return failures


def works_before_the_close(cache: Cache) -> Body:
    def first() -> None:
        assert cache.get('before') == 'parent'
        assert cache.set('early', 1) is True

    return first


def works_after_the_close(cache: Cache) -> Body:
    def second() -> None:
        assert cache.get('new') == 'visible', "the child doesn't see another process's write"
        assert [cache.set(f'late{i}', i) for i in range(3)] == [True, True, True]
        assert [cache.get(f'late{i}') for i in range(3)] == [0, 1, 2]

    return second


def nothing_lost(directory: Path) -> list[str]:
    """The child's writes, early and late, are in the database a fresh handle opens."""
    with Cache(directory, timeout=TIMEOUT) as fresh:
        got = (fresh.get('early'), [fresh.get(f'late{i}') for i in range(3)], fresh.get('new'), fresh.check())
    return [] if got == (1, [0, 1, 2], 'visible', []) else [f'a fresh handle found (early, late, new, check) = {got}']


def refuses_everything(cache: Cache, directory: Path) -> Body:
    def refused() -> None:
        unusable('get', lambda: cache.get('before'))
        unusable('in', lambda: 'before' in cache)
        unusable('set', lambda: cache.set('child', 1))
        unusable('delete', lambda: cache.delete('before'))
        unusable('len', lambda: len(cache))
        unusable('a fresh Cache', lambda: Cache(directory, timeout=TIMEOUT))

    return refused


def untouched(directory: Path, extra: Optional[dict[str, object]] = None) -> list[str]:
    """A child that could only raise changed nothing, and the parent's data is all there."""
    want: dict[str, object] = {'before': 'parent', 'new': 'visible', 'child': None, **(extra or {})}
    with Cache(directory, timeout=TIMEOUT) as fresh:
        got = {key: fresh.get(key) for key in want}
        findings = fresh.check()
    failures = [] if got == want else [f'a fresh handle found {got}, expected {want}']
    return [*failures, *[f'check found {finding}' for finding in findings]]


def untouched_but_for(directory: Path, extra: dict[str, object]) -> list[str]:
    """`untouched`, for a scenario with no third process: nothing wrote `new`."""
    return untouched(directory, {'new': None, **extra})


def later_before_hook_uses_the_cache(directory: Path) -> list[str]:
    """Another library's before-fork hook, running after diskcache-oxide's, reads and writes the
    cache. Its connection is closed when it's given back, not kept idle, so the child inherits
    nothing: it works, and its writes survive the parent's close."""
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    used: list[object] = []

    def hook() -> None:
        used.append(cache.get('before'))
        used.append(cache.set('hooked', 1))

    LATE_BEFORE.append(hook)
    failures = across_a_parent_close(
        directory, fork, works_before_the_close(cache), works_after_the_close(cache), cache.close
    )
    LATE_BEFORE.clear()
    if used != ['parent', True]:
        failures.append(f"the hook's calls answered {used}")
    with Cache(directory, timeout=TIMEOUT) as fresh:
        if fresh.get('hooked') != 1:
            failures.append("the hook's write is missing")
    return failures + nothing_lost(directory)


def later_before_hook_opens_a_cache(directory: Path) -> list[str]:
    """The later before-fork hook opens a cache of its own (and a second handle on the first). The
    connection an open leaves in the pool is closed there too, so the child can use both."""
    other = directory.parent / f'{directory.name}-opened-in-the-hook'
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    opened: list[Cache] = []

    def hook() -> None:
        opened.append(Cache(other, timeout=TIMEOUT))
        opened[0].set('before', 'parent')
        opened.append(Cache(directory, timeout=TIMEOUT))

    LATE_BEFORE.append(hook)

    def first() -> None:
        works_before_the_close(cache)()
        works_before_the_close(opened[0])()
        assert opened[1].get('before') == 'parent'

    def second() -> None:
        works_after_the_close(cache)()
        works_after_the_close(opened[0])()

    def close() -> None:
        for handle in (cache, *opened):
            handle.close()
        third_process(other)

    failures = across_a_parent_close(directory, fork, first, second, close)
    LATE_BEFORE.clear()
    return failures + nothing_lost(directory) + nothing_lost(other)


def fork_inside_a_transaction(directory: Path) -> list[str]:
    """`os.fork()` inside `with cache.transact():`. The held connection is inherited, so the child
    can't use the cache, before or after the parent's close, and the parent loses nothing."""
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    hold = cache.transact()
    hold.__enter__()
    cache.set('in-hold', 1)

    def first() -> None:
        # Still inside the inherited block: the same error as everywhere else on this cache.
        unusable('read inside the inherited transaction', lambda: cache.get('before'))
        unusable('write inside the inherited transaction', lambda: cache.set('child', 1))
        fails_fast('leaving the inherited transaction', lambda: hold.__exit__(None, None, None))
        refuses_everything(cache, directory)()

    def close() -> None:
        hold.__exit__(None, None, None)
        cache.close()

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        failures = across_a_parent_close(directory, fork, first, refuses_everything(cache, directory), close)
    return failures + untouched(directory, {'in-hold': 1})


def fork_inside_a_nested_transaction(directory: Path) -> list[str]:
    """`os.fork()` inside a nested `with cache.transact():` (PassThrough). Leaving the inner
    transaction in the child—normally, with an exception, or from an early after_in_child hook—must
    raise ForkedInsideTransactionError rather than silently succeeding as Done(committed)."""
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    outer = cache.transact()
    outer.__enter__()
    cache.set('in-hold', 1)
    inner = cache.transact()
    inner.__enter__()
    inner_exc = cache.transact()
    inner_exc.__enter__()
    early_inner = cache.transact()
    early_inner.__enter__()
    in_hook: list[str] = []

    def early() -> None:
        fails_fast('leaving nested tx in early hook', lambda: early_inner.__exit__(None, None, None))
        in_hook.append('ok')

    def fork_behind_an_early_hook(child: Body) -> int:
        EARLY_CHILD.append(early)
        try:
            return fork(child)
        finally:
            EARLY_CHILD.clear()

    def first() -> None:
        assert in_hook == ['ok'], in_hook
        fails_fast('leaving nested tx', lambda: inner.__exit__(None, None, None))
        fails_fast('leaving nested tx with exception', lambda: inner_exc.__exit__(ValueError, ValueError('x'), None))
        fails_fast('leaving outer tx', lambda: outer.__exit__(None, None, None))
        refuses_everything(cache, directory)()

    def close() -> None:
        early_inner.__exit__(None, None, None)
        inner_exc.__exit__(None, None, None)
        inner.__exit__(None, None, None)
        outer.__exit__(None, None, None)
        cache.close()

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        failures = across_a_parent_close(
            directory, fork_behind_an_early_hook, first, refuses_everything(cache, directory), close
        )
    return failures + untouched(directory, {'in-hold': 1})


def fork_while_entering_a_transaction(directory: Path) -> list[str]:
    """Another thread is blocked in `tx.__enter__()` (`TxState::Entering`) while the owner thread
    holds an outer transaction and forks. In the child—both in an early after_in_child hook and
    afterwards—`repr(tx)` must not hang, `tx.__enter__()` must raise RuntimeError, and `tx.__exit__`
    must fail fast with ForkedInsideTransactionError."""
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    outer = cache.transact()
    outer.__enter__()
    cache.set('in-hold', 1)
    entering = cache.transact()
    worker_errors: list[BaseException] = []

    def enter_and_exit() -> None:
        try:
            entering.__enter__()
            entering.__exit__(None, None, None)
        except BaseException as exc:  # noqa: BLE001
            worker_errors.append(exc)

    waiter = threading.Thread(target=enter_and_exit)
    waiter.start()
    deadline = time.monotonic() + TIMEOUT
    while 'Entering' not in repr(entering):
        if time.monotonic() >= deadline:
            outer.__exit__(None, None, None)
            waiter.join(timeout=TIMEOUT)
            cache.close()
            return [f'worker never reached Entering state: {repr(entering)}']
        time.sleep(0.005)

    in_hook: list[str] = []

    def early() -> None:
        assert repr(entering) == '<diskcache_oxide.Transaction Entering>', repr(entering)
        fails_fast('exiting Entering tx in early hook', lambda: entering.__exit__(None, None, None))
        in_hook.append('ok')

    def fork_behind_an_early_hook(child: Body) -> int:
        EARLY_CHILD.append(early)
        try:
            return fork(child)
        finally:
            EARLY_CHILD.clear()

    def first() -> None:
        assert in_hook == ['ok'], in_hook
        assert repr(entering) == '<diskcache_oxide.Transaction Entering>', repr(entering)
        try:
            entering.__enter__()
        except RuntimeError as exc:
            assert 'entered only once' in str(exc), str(exc)
        else:
            raise AssertionError('re-entering an Entering tx in child unexpectedly succeeded')
        fails_fast('exiting Entering tx', lambda: entering.__exit__(None, None, None))
        fails_fast('exiting Entering tx with exception', lambda: entering.__exit__(ValueError, ValueError('x'), None))
        fails_fast('leaving outer tx', lambda: outer.__exit__(None, None, None))
        refuses_everything(cache, directory)()

    def close() -> None:
        outer.__exit__(None, None, None)
        waiter.join(timeout=TIMEOUT)
        cache.close()

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        failures = across_a_parent_close(
            directory, fork_behind_an_early_hook, first, refuses_everything(cache, directory), close
        )
    if worker_errors:
        failures.append(f'parent entering worker raised: {worker_errors!r}')
    return failures + untouched(directory, {'in-hold': 1})


def fork_from_a_hook_inside_get(directory: Path) -> list[str]:
    """`os.fork()` from a decoder running inside `get()`, whose connection is leased across the
    fork. Nothing poisoned that child before plan 32: its sets returned True and were lost. Now it
    can't use the cache, and another directory works."""
    other = directory.parent / f'{directory.name}-other'
    register_type(ForkOnDecode, name='tests.ForkOnDecode', encode=lambda v: v.n, decode=fork_on_decode)
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    cache.set('forker', ForkOnDecode(7))
    got: list[object] = []

    def fork_inside_get(child: Body) -> int:
        pids: list[int] = []
        ForkOnDecode.hook = lambda: pids.append(fork(child))
        got.append(cache.get('forker'))
        return pids[0]

    def first() -> None:
        refuses_everything(cache, directory)()
        with Cache(other, timeout=TIMEOUT) as elsewhere:
            assert elsewhere.set('child', 1) is True and elsewhere.get('child') == 1

    def second() -> None:
        refuses_everything(cache, directory)()
        still_answers(cache, directory)

    failures = across_a_parent_close(directory, fork_inside_get, first, second, cache.close)
    unregister_type(ForkOnDecode)
    if not (len(got) == 1 and isinstance(got[0], ForkOnDecode) and got[0].n == 7):
        failures.append(f"the parent's own get returned {got}")
    with Cache(other, timeout=TIMEOUT) as elsewhere:
        if elsewhere.get('child') != 1:
            failures.append("the child's write to another directory is missing")
    return failures + untouched(directory)


def child_returns_from_the_inherited_get(directory: Path) -> list[str]:
    """The same fork from a decoder inside `get()`, but the child returns from the decoder instead of
    exiting inside it: the inherited `get` finishes on the parent's connection (the row was already
    read), hands back the value, and gives the connection back without closing it. Every call after
    that raises, the child exits normally, and the parent's data is untouched."""
    register_type(ForkOnDecode, name='tests.ForkOnDecode', encode=lambda v: v.n, decode=fork_on_decode)
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    cache.set('forker', ForkOnDecode(7))
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    pids: list[int] = []
    ForkOnDecode.hook = lambda: pids.append(os.fork())
    got = cache.get('forker')
    if pids[0] == 0:
        code = 0
        try:
            assert isinstance(got, ForkOnDecode) and got.n == 7, got
            refuses_everything(cache, directory)()
            os.write(ready_w, b'r')
            assert os.read(go_r, 1) == b'g'
            refuses_everything(cache, directory)()
            still_answers(cache, directory)
        except BaseException:
            traceback.print_exc()
            code = 1
        finally:
            os._exit(code)
    os.close(ready_w)
    failures = []
    if os.read(ready_r, 1) != b'r':
        failures.append("the child failed before the parent's close")
    cache.close()
    third_process(directory)
    try:
        os.write(go_w, b'g')
    except BrokenPipeError:
        pass
    code = wait(pids[0])
    if code != 0:
        failures.append(f'child exit code {code}')
    unregister_type(ForkOnDecode)
    if not (isinstance(got, ForkOnDecode) and got.n == 7):
        failures.append(f"the parent's own get returned {got!r}")
    return failures + untouched(directory)


def early_child_hook_inside_an_operation(directory: Path) -> list[str]:
    """Another library's `after_in_child` hook, running before diskcache-oxide's, uses the cache in
    a child forked from a decoder inside `get()`. The forking thread is one gate level deep there,
    and the hook's call must still run the child-side reset first: it's refused, instead of opening
    a connection that takes no kernel lock. A cache nothing was open on works in the same hook."""
    other = directory.parent / f'{directory.name}-other'
    register_type(ForkOnDecode, name='tests.ForkOnDecode', encode=lambda v: v.n, decode=fork_on_decode)
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    cache.set('forker', ForkOnDecode(7))
    in_hook: list[str] = []

    def hook() -> None:
        try:
            unusable('get in the early hook', lambda: cache.get('before'))
            unusable('set in the early hook', lambda: cache.set('child', 1))
            with Cache(other, timeout=TIMEOUT) as elsewhere:
                assert elsewhere.set('hooked', 1) is True
            in_hook.append('ok')
        except BaseException:
            in_hook.append(traceback.format_exc())

    def fork_inside_get(child: Body) -> int:
        pids: list[int] = []
        ForkOnDecode.hook = lambda: pids.append(fork(child))
        EARLY_CHILD.append(hook)
        cache.get('forker')
        EARLY_CHILD.clear()
        return pids[0]

    def first() -> None:
        assert in_hook == ['ok'], in_hook
        refuses_everything(cache, directory)()

    failures = across_a_parent_close(
        directory, fork_inside_get, first, refuses_everything(cache, directory), cache.close
    )
    unregister_type(ForkOnDecode)
    with Cache(other, timeout=TIMEOUT) as elsewhere:
        if elsewhere.get('hooked') != 1:
            failures.append("the early hook's write to another directory is missing")
    return failures + untouched(directory)


def holders() -> int:
    """How many fork-gate levels stay held in this process (the test hook `_gate_stats`): the lowest
    count seen within a second, returning at the first 0. The maintenance thread, restarted after
    every fork in the parent and in the child, holds a level for a moment at the start of each pass
    (ThreadSanitizer's lane caught one, 2026-11-20). A stranded level never goes away."""
    deadline = time.monotonic() + 1.0
    lowest = _core._gate_stats()['holders']
    while lowest != 0 and time.monotonic() < deadline:
        time.sleep(0.001)
        lowest = min(lowest, _core._gate_stats()['holders'])
    return lowest


def a_child_with_no_stranded_level(cache: Cache, hold: object, directory: Path, other: Path) -> None:
    """What the child of a fork that landed mid-end must find (plan 32, bug B): the transaction's gate
    level, stranded on a thread the child doesn't have, counts for nothing."""
    assert holders() == 0, f'{holders()} gate levels are held in a child that holds none'
    assert 'Ending' in repr(hold), repr(hold)
    # At once: no waiting out the timeout for an end that can never finish here.
    fails_fast('leaving the inherited transaction', lambda: hold.__exit__(None, None, None))  # ty: ignore[unresolved-attribute]
    fails_fast('leaving it again', lambda: hold.__exit__(None, None, None))  # ty: ignore[unresolved-attribute]
    assert holders() == 0
    # Another cache waits for locks as in any process, not as if inside a transaction.
    with Cache(other, timeout=TIMEOUT) as elsewhere:
        start = time.monotonic()
        assert elsewhere.set('child', 1) is True
        assert time.monotonic() - start < 1.0
    # A fork from another thread of the child doesn't wait for the stranded level.
    codes: list[Optional[int]] = []
    forker = threading.Thread(target=lambda: codes.append(wait(fork(lambda: None))), daemon=True)
    forker.start()
    forker.join(TIMEOUT)
    assert not forker.is_alive(), 'os.fork() from another thread hung on a level nothing can release'
    assert codes == [0], codes
    refuses_everything(cache, directory)()


def wait_until_ending(hold: object) -> None:
    """Returns once another thread has taken `hold` to end it and is waiting: `Ending` shows only
    after that thread released the interpreter (the GIL build), and the fork itself waits for it to
    (the free-threaded build's stop-the-world). So the fork that follows lands inside the end."""
    deadline = time.monotonic() + TIMEOUT
    while 'Ending' not in repr(hold):
        assert time.monotonic() < deadline, f'the other thread never began the end: {hold!r}'
        time.sleep(0.001)


def end_on_another_thread_behind_the_fork(directory: Path) -> list[str]:
    """Thread B leaves the main thread's `with cache.transact():` block while the main thread is
    forking: B has taken the transaction and waits to get into the gate behind that fork. In the
    child B doesn't exist. Before plan 32 the level it was ending stayed counted there for good: a
    later `os.fork()` from another thread hung, and the main thread's `__exit__` waited out its
    timeout. A before-fork hook holds the fork open until B is there."""
    other = directory.parent / f'{directory.name}-other'
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    hold = cache.transact()
    hold.__enter__()
    cache.set('in-hold', 1)
    in_hook = threading.Event()
    ended: list[object] = []

    def hook() -> None:
        in_hook.set()
        wait_until_ending(hold)  # The fork is claimed and not yet done: B's exit waits behind it.

    def ender() -> None:
        in_hook.wait(TIMEOUT)
        try:
            hold.__exit__(None, None, None)
            ended.append('committed')
        except BaseException as ex:
            ended.append(ex)

    thread = threading.Thread(target=ender)
    thread.start()
    LATE_BEFORE.append(hook)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        pid = fork(lambda: a_child_with_no_stranded_level(cache, hold, directory, other))
    LATE_BEFORE.clear()
    code = wait(pid)
    thread.join(TIMEOUT * 2)
    failures = []
    if code != 0:
        failures.append(f'child exit code {code}')
    if ended != ['committed']:
        failures.append(f"the other thread's exit in the parent: {ended}")
    if holders() != 0:
        failures.append(f'the parent is left with {holders()} gate levels')
    cache.close()
    return failures + untouched_but_for(directory, {'in-hold': 1})


def end_on_another_thread_while_the_owner_is_inside_a_call(directory: Path) -> list[str]:
    """The other way in: the owner is inside one of the transaction's own calls (a decoder running
    inside `get()`), so thread B's commit polls, with no gate level, for that call to hand the
    connection back. The owner forks from the decoder meanwhile; B is in that poll when the fork is
    claimed, and waiting behind the fork by the time it lands. The child, whose connection slot is
    lent to a call it is still inside, returns from the `get` and finds no stranded level; in the
    parent B commits once the call returns."""
    other = directory.parent / f'{directory.name}-other'
    register_type(ForkOnDecode, name='tests.ForkOnDecode', encode=lambda v: v.n, decode=fork_on_decode)
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    cache.set('forker', ForkOnDecode(7))
    hold = cache.transact()
    hold.__enter__()
    cache.set('in-hold', 1)
    ended: list[object] = []
    pids: list[int] = []

    def ender() -> None:
        try:
            hold.__exit__(None, None, None)
            ended.append('committed')
        except BaseException as ex:
            ended.append(ex)

    thread = threading.Thread(target=ender)

    def hook() -> None:
        thread.start()
        wait_until_ending(hold)  # B has taken the transaction and is waiting for this call.
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            pids.append(os.fork())

    ForkOnDecode.hook = hook
    got = cache.get('forker')
    if pids[0] == 0:
        code = 0
        try:
            assert isinstance(got, ForkOnDecode) and got.n == 7, got
            a_child_with_no_stranded_level(cache, hold, directory, other)
        except BaseException:
            traceback.print_exc()
            code = 1
        finally:
            os._exit(code)
    code = wait(pids[0])
    thread.join(TIMEOUT * 2)
    unregister_type(ForkOnDecode)
    failures = []
    if code != 0:
        failures.append(f'child exit code {code}')
    if ended != ['committed']:
        failures.append(f"the other thread's exit in the parent: {ended}")
    if holders() != 0:
        failures.append(f'the parent is left with {holders()} gate levels')
    cache.close()
    return failures + untouched_but_for(directory, {'in-hold': 1})


def early_child_hook_on_a_transaction_being_ended(directory: Path) -> list[str]:
    """As `end-on-another-thread-behind-the-fork`, with another library's `after_in_child` hook
    running before diskcache-oxide's and touching the transaction and `stats()`, neither of which
    enters the gate (Gemini's review of plan 32, H1). Before the fix they saw the parent's
    generation there: `__exit__` waited out the timeout for the parent's thread and raised
    `Timeout`, an exit with an exception returned False, and `stats()` answered."""
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    hold = cache.transact()
    hold.__enter__()
    cache.set('in-hold', 1)
    in_before = threading.Event()
    ended: list[object] = []
    in_child: list[str] = []

    def before() -> None:
        in_before.set()
        wait_until_ending(hold)

    def ender() -> None:
        in_before.wait(TIMEOUT)
        try:
            hold.__exit__(None, None, None)
            ended.append('committed')
        except BaseException as ex:
            ended.append(ex)

    def early() -> None:
        try:
            # The exit first: it must bring the child up to date itself, not ride on `stats()` having.
            fails_fast('leaving the transaction in the early hook', lambda: hold.__exit__(None, None, None))
            unusable('stats in the early hook', cache.stats)
            unusable('stats with a reset in the early hook', lambda: cache.stats(reset=True))
            fails_fast('leaving it on an exception', lambda: hold.__exit__(ValueError, ValueError(), None))
            try:
                hold.__enter__()
            except RuntimeError as ex:
                assert 'only once' in str(ex), ex
            else:
                raise AssertionError('the inherited transaction was entered again')
            assert 'Transaction' in repr(hold)
            in_child.append('ok')
        except BaseException:
            in_child.append(traceback.format_exc())

    def child() -> None:
        assert in_child == ['ok'], in_child
        refuses_everything(cache, directory)()

    thread = threading.Thread(target=ender)
    thread.start()
    LATE_BEFORE.append(before)
    EARLY_CHILD.append(early)
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        pid = fork(child)
    LATE_BEFORE.clear()
    EARLY_CHILD.clear()
    code = wait(pid)
    thread.join(TIMEOUT * 2)
    failures = []
    if code != 0:
        failures.append(f'child exit code {code}')
    if ended != ['committed']:
        failures.append(f"the other thread's exit in the parent: {ended}")
    cache.close()
    return failures + untouched_but_for(directory, {'in-hold': 1})


def rolled_back_on_another_thread_before_the_fork(directory: Path) -> list[str]:
    """Thread B rolls the transaction back while its owner is inside one of its calls (a decoder
    inside `get()`): a rollback doesn't wait for the call, so B is done, and gone, before the owner
    forks from that decoder. The connection is still lent to the call, so the child can't use the
    cache, and leaving the inherited `with` block must say so (Gemini's review of plan 32, H1). It
    raised `TransactionAborted` there, or returned False on an exception, as if the parent's
    rollback were the child's news."""
    register_type(ForkOnDecode, name='tests.ForkOnDecode', encode=lambda v: v.n, decode=fork_on_decode)
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    cache.set('forker', ForkOnDecode(7))
    hold = cache.transact()
    hold.__enter__()
    cache.set('in-hold', 1)
    ended: list[object] = []
    pids: list[int] = []

    def ender() -> None:
        try:
            ended.append(hold.__exit__(ValueError, ValueError('rolled back'), None))
        except BaseException as ex:
            ended.append(ex)

    def hook() -> None:
        thread = threading.Thread(target=ender)
        thread.start()
        thread.join(TIMEOUT)
        assert not thread.is_alive(), 'a rollback from another thread waited for the call'
        assert 'Done(rolled back)' in repr(hold), repr(hold)
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            pids.append(os.fork())

    ForkOnDecode.hook = hook
    got = cache.get('forker')
    if pids[0] == 0:
        code = 0
        try:
            assert isinstance(got, ForkOnDecode) and got.n == 7, got
            fails_fast('leaving the inherited transaction', lambda: hold.__exit__(None, None, None))
            fails_fast('leaving it on an exception', lambda: hold.__exit__(ValueError, ValueError(), None))
            refuses_everything(cache, directory)()
        except BaseException:
            traceback.print_exc()
            code = 1
        finally:
            os._exit(code)
    code = wait(pids[0])
    unregister_type(ForkOnDecode)
    failures = []
    if code != 0:
        failures.append(f'child exit code {code}')
    if ended != [False]:
        failures.append(f"the other thread's rollback in the parent: {ended}")
    # The parent's own exit is unchanged: its transaction was rolled back by the other thread.
    try:
        hold.__exit__(None, None, None)
        failures.append("the owner's exit in the parent reported a commit")
    except RuntimeError as ex:
        if type(ex).__name__ != 'TransactionAborted':
            failures.append(f"the owner's exit in the parent: {type(ex).__name__}: {ex}")
    if holders() != 0:
        failures.append(f'the parent is left with {holders()} gate levels')
    cache.close()
    return failures + untouched_but_for(directory, {})


def fanout_with_one_shard_held(directory: Path) -> list[str]:
    """`os.fork()` inside `fanout.transact(key=...)`: one shard's connection is inherited. In the
    child that shard's keys raise, keys on every other shard work and persist, a call that covers
    every shard raises, and a fresh `FanoutCache` on the root can't be opened (plan 32, decision 6)."""
    cache = FanoutCache(directory, shards=4, timeout=TIMEOUT)
    held_key, *free_keys = one_key_per_shard(cache, 'k').values()
    for key in (held_key, *free_keys):
        cache.set(key, 'parent')
    hold = cache.transact(key=held_key)
    hold.__enter__()
    cache.set(held_key, 'in-hold')

    def held_shard_refuses() -> None:
        unusable('get on the held shard', lambda: cache.get(held_key))
        unusable('set on the held shard', lambda: cache.set(held_key, 'child'))
        unusable('delete on the held shard', lambda: cache.delete(held_key))

    in_hook: list[str] = []

    def early() -> None:
        # Another library's `after_in_child` hook, before ours: a call over every shard is refused
        # before it touches one. `clear()` here used to empty the shards ahead of the held one.
        try:
            for name, call in {'clear': cache.clear, 'stats': cache.stats, 'len': lambda: len(cache)}.items():
                unusable(f'{name} in an early hook', call)
            in_hook.append('ok')
        except BaseException:
            in_hook.append(traceback.format_exc())

    def fork_behind_an_early_hook(child: Body) -> int:
        EARLY_CHILD.append(early)
        try:
            return fork(child)
        finally:
            EARLY_CHILD.clear()

    def first() -> None:
        assert in_hook == ['ok'], in_hook
        held_shard_refuses()
        fails_fast('leaving the inherited transaction', lambda: hold.__exit__(None, None, None))
        held_shard_refuses()
        for key in free_keys:
            assert cache.get(key) == 'parent', key
            assert cache.set(key, 'child-early') is True, key
        whole: dict[str, Callable[[], object]] = {
            'len': lambda: len(cache),
            'iteration': lambda: list(cache),
            'volume': cache.volume,
            'stats': cache.stats,
            'expire': cache.expire,
            'cull': cache.cull,
            'clear': cache.clear,
            'check': cache.check,
            'checkpoint': cache.checkpoint,
            'evict': lambda: cache.evict('tag'),
            'evict of no tag': lambda: cache.evict(None),
            'remove_old_formats': cache.remove_old_formats,
            'reset': lambda: cache.reset('cull_limit'),
            'iterkeys': lambda: list(cache.iterkeys()),
            'reversed': lambda: list(reversed(cache)),
            'a fresh FanoutCache': lambda: FanoutCache(directory, shards=4, timeout=TIMEOUT),
            # With limits: it used to write them to the shards ahead of the held one, then raise.
            'a fresh FanoutCache with limits': lambda: FanoutCache(
                directory, shards=4, timeout=TIMEOUT, cull_limit=3, eviction_policy='none'
            ),
        }
        for name, call in whole.items():
            unusable(name, call)
        assert cache.cull_limit == 10 and cache.eviction_policy == 'least-recently-stored', (
            cache.cull_limit,
            cache.eviction_policy,
        )

    def second() -> None:
        held_shard_refuses()
        for key in free_keys:
            assert cache.get(key) == 'child-early', key
            assert cache.set(key, 'child-late') is True, key
        still_answers(cache, directory)

    def close() -> None:
        hold.__exit__(None, None, None)
        cache.close()

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        failures = across_a_parent_close(directory, fork_behind_an_early_hook, first, second, close, fanout=True)
    with FanoutCache(directory, shards=4, timeout=TIMEOUT) as fresh:
        got = (fresh.get(held_key), [fresh.get(key) for key in free_keys], fresh.get('new'), fresh.check())
        if (fresh.cull_limit, fresh.eviction_policy) != (10, 'least-recently-stored'):
            failures.append(f"the child's refused open changed the limits: {fresh.cull_limit}, {fresh.eviction_policy}")
    want = ('in-hold', ['child-late'] * len(free_keys), 'visible', [])
    if got != want:
        failures.append(f'a fresh handle found (held, free, new, check) = {got}, expected {want}')
    return failures


SCENARIOS = {
    'later-before-hook-uses-the-cache': later_before_hook_uses_the_cache,
    'later-before-hook-opens-a-cache': later_before_hook_opens_a_cache,
    'fork-inside-a-transaction': fork_inside_a_transaction,
    'fork-inside-a-nested-transaction': fork_inside_a_nested_transaction,
    'fork-while-entering-a-transaction': fork_while_entering_a_transaction,
    'fork-from-a-hook-inside-get': fork_from_a_hook_inside_get,
    'child-returns-from-the-inherited-get': child_returns_from_the_inherited_get,
    'early-child-hook-inside-an-operation': early_child_hook_inside_an_operation,
    'fanout-with-one-shard-held': fanout_with_one_shard_held,
    'end-on-another-thread-behind-the-fork': end_on_another_thread_behind_the_fork,
    'end-on-another-thread-while-the-owner-is-inside-a-call': end_on_another_thread_while_the_owner_is_inside_a_call,
    'early-child-hook-on-a-transaction-being-ended': early_child_hook_on_a_transaction_being_ended,
    'rolled-back-on-another-thread-before-the-fork': rolled_back_on_another_thread_before_the_fork,
}


def main() -> int:
    name, directory = sys.argv[1], Path(sys.argv[2])
    set_title(f'fork-{name}')
    from tests.maintenance import apply_from_env

    apply_from_env()
    # Forking with threads running is the point of two of these scenarios.
    warnings.filterwarnings('ignore', r'This process .* is multi-threaded', DeprecationWarning)
    failures = SCENARIOS[name](directory)
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
