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
import traceback  # noqa: E402
import warnings  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import Optional  # noqa: E402

from diskcache_oxide import Cache, FanoutCache, register_type, unregister_type  # noqa: E402

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

    def first() -> None:
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
            'a fresh FanoutCache': lambda: FanoutCache(directory, shards=4, timeout=TIMEOUT),
        }
        for name, call in whole.items():
            unusable(name, call)

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
        failures = across_a_parent_close(directory, fork, first, second, close, fanout=True)
    with FanoutCache(directory, shards=4, timeout=TIMEOUT) as fresh:
        got = (fresh.get(held_key), [fresh.get(key) for key in free_keys], fresh.get('new'), fresh.check())
    want = ('in-hold', ['child-late'] * len(free_keys), 'visible', [])
    if got != want:
        failures.append(f'a fresh handle found (held, free, new, check) = {got}, expected {want}')
    return failures


SCENARIOS = {
    'later-before-hook-uses-the-cache': later_before_hook_uses_the_cache,
    'later-before-hook-opens-a-cache': later_before_hook_opens_a_cache,
    'fork-inside-a-transaction': fork_inside_a_transaction,
    'fork-from-a-hook-inside-get': fork_from_a_hook_inside_get,
    'early-child-hook-inside-an-operation': early_child_hook_inside_an_operation,
    'fanout-with-one-shard-held': fanout_with_one_shard_held,
}


def main() -> int:
    name, directory = sys.argv[1], Path(sys.argv[2])
    set_title(f'fork-{name}')
    from tests.maintenance import apply_from_env

    apply_from_env()
    failures = SCENARIOS[name](directory)
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
