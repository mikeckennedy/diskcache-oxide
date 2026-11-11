"""Fork scenarios for test_concurrency.py, each run in a fresh process (never fork pytest itself).

    python -m tests.unit.fork_scenarios <scenario> <directory>

Exits 0 if every check passed; otherwise prints the failures to stderr and exits 1. Children report
through their exit code: 0 passed, 1 a check failed (traceback on stderr), None hung and was killed.
No set_title() after a fork: setproctitle hangs in a child forked from a multi-threaded process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import traceback
import warnings
from collections.abc import Callable
from pathlib import Path

from diskcache_oxide import Cache, FanoutCache, Timeout, register_type, unregister_type

from tests.conformance.proc_workers import SqlitePeer
from tests.keys import str_keys
from tests.proctitle import set_title
from tests.unit.fork_timeout_scenarios import fds_inside

CHILD_DEADLINE = 15.0
# A write that must fail at once rather than wait out the cache's timeout (TIMEOUT).
FAST = 0.5
TIMEOUT = 5.0


def fork(body: Callable[[], None]) -> int:
    pid = os.fork()
    if pid == 0:
        code = 0
        try:
            body()
        except BaseException:
            traceback.print_exc()
            code = 1
        finally:
            os._exit(code)
    return pid


def wait(pid: int, deadline: float = CHILD_DEADLINE) -> int | None:
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.01)
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    return None


def fails_fast(what: str, call: Callable[[], object]) -> None:
    """`call()` raises RuntimeError well before the cache's timeout (D-020)."""
    start = time.monotonic()
    try:
        call()
    except RuntimeError:
        elapsed = time.monotonic() - start
        assert elapsed < FAST, f'{what}: RuntimeError only after {elapsed:.2f}s'
    else:
        raise AssertionError(f'{what}: expected RuntimeError')


def d020(directory: Path) -> list[str]:
    """Fork inside this thread's own held transaction (D-020).

    The child can never write that database: through the inherited Cache, a fresh Cache on the same
    directory, or in a grandchild, each write raises RuntimeError at once. Operations inside the
    inherited transaction and leaving it raise too. Reads outside it, and other directories, work.
    The parent gets a RuntimeWarning, and its transaction commits normally.
    """
    other = directory.parent / f'{directory.name}-other'
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    queued = cache.push('queued')
    hold = cache.transact()
    hold.__enter__()
    cache.set('in-hold', 1)

    def grandchild() -> None:
        fails_fast(
            'grandchild write, fresh Cache',
            lambda: Cache(directory, timeout=TIMEOUT).set('grandchild', 1),
        )
        assert Cache(directory, timeout=TIMEOUT).get('before') == 'parent'
        with Cache(other, timeout=TIMEOUT) as elsewhere:
            assert elsewhere.set('grandchild', 1) is True and elsewhere.get('grandchild') == 1

    def child() -> None:
        fails_fast('write inside the inherited transaction', lambda: cache.set('child', 1))
        fails_fast('read inside the inherited transaction', lambda: cache.get('before'))
        fails_fast('leaving the inherited transaction', lambda: hold.__exit__(None, None, None))
        assert cache.get('before') == 'parent', 'a read after leaving the transaction'
        assert cache.get('in-hold') is None, "the parent's uncommitted write is visible"
        fails_fast('write through the inherited Cache', lambda: cache.set('child', 1))
        fails_fast('pop of a key the child can read', lambda: cache.pop('before'))
        fails_fast('pop of a missing key', lambda: cache.pop('missing'))
        assert cache.get('before') == 'parent', 'the refused pop removed nothing'
        fails_fast('pull from a queue', cache.pull)
        fails_fast('push onto a queue', lambda: cache.push('child'))
        assert cache.peek() == (queued, 'queued'), 'peek still reads, and the refused pull removed nothing'
        fresh = Cache(directory, timeout=TIMEOUT)
        assert fresh.get('before') == 'parent'
        fails_fast('write through a fresh Cache', lambda: fresh.set('child', 1))
        fails_fast('checkpoint through a fresh Cache', fresh.checkpoint)
        with Cache(other, timeout=TIMEOUT) as elsewhere:
            assert elsewhere.set('child', 1) is True and elsewhere.get('child') == 1
        code = wait(fork(grandchild))
        assert code == 0, f'grandchild exit code {code}'

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        pid = fork(child)
    code = wait(pid)
    hold.__exit__(None, None, None)

    failures = []
    if code != 0:
        failures.append(f'child exit code {code}')
    if not any(issubclass(w.category, RuntimeWarning) for w in caught):
        failures.append('the parent got no RuntimeWarning for forking inside its transaction')
    if cache.get('in-hold') != 1:
        failures.append("the parent's held transaction did not commit")
    if cache.get('child') is not None or cache.get('grandchild') is not None:
        failures.append('a poisoned process wrote to the database')
    with Cache(other) as elsewhere:
        if elsewhere.get('child') != 1 or elsewhere.get('grandchild') != 1:
            failures.append('the other directory is missing the child or grandchild write')
    cache.close()
    return failures


def fork_while_a_write_waits(directory: Path) -> list[str]:
    """Fork while another thread waits for SQLite's write lock (held by another process).

    That thread holds the in-process write lock and sleeps in the detached busy retry, outside the
    fork gate, so the fork must not wait for it (fork latency). The child inherits the write lock
    held by a thread it doesn't have; it must treat it as free, so its own write goes through once
    the other process lets go instead of timing out.
    """
    hold_for = 2.0
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    failures: list[str] = []
    with SqlitePeer(directory / 'diskcache-oxide.sqlite') as peer:
        peer.begin('IMMEDIATE')
        released = time.monotonic() + hold_for
        waiter_errors: list[BaseException] = []

        def waiter() -> None:
            try:
                cache.set('waiter', 1)
            except BaseException as ex:
                waiter_errors.append(ex)

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.3)  # Well into the detached retry.

        def child() -> None:
            assert cache.get('before') == 'parent'
            cache.set('child', 1)  # Waits for the peer, then must not wait for the write lock.
            assert time.monotonic() >= released - 0.1, 'the child wrote while another process held the lock'

        start = time.monotonic()
        pid = fork(child)
        latency = time.monotonic() - start
        time.sleep(max(0.0, released - time.monotonic()))
        peer.rollback()
        thread.join(TIMEOUT + 1)
        code = wait(pid)

    if latency > 0.25:
        failures.append(f'fork took {latency:.3f}s while a write waited for the lock')
    if code != 0:
        failures.append(f'child exit code {code}')
    if waiter_errors:
        failures.append(f'the waiting write failed: {waiter_errors[0]!r}')
    if cache.get('waiter') != 1 or cache.get('child') != 1:
        failures.append(f'missing writes: waiter={cache.get("waiter")!r} child={cache.get("child")!r}')
    cache.close()
    return failures


def fork_while_a_write_retries(directory: Path) -> list[str]:
    """Fork while another thread's `set(retry=True)` keeps timing out behind another process's lock.

    Between rounds the retrying thread holds nothing (no gate level, no connection, no write lock),
    so the fork must not wait for it, and its retry must not be starved by the fork. The child can
    write once the other process lets go, and the retried write lands in the parent.
    """
    hold_for = 1.5
    cache = Cache(directory, timeout=0.05)
    cache.set('before', 'parent')
    failures: list[str] = []
    with SqlitePeer(directory / 'diskcache-oxide.sqlite') as peer:
        peer.begin('IMMEDIATE')
        released = time.monotonic() + hold_for
        errors: list[BaseException] = []

        def retrier() -> None:
            try:
                cache.set('retried', 1, retry=True)
            except BaseException as ex:
                errors.append(ex)

        thread = threading.Thread(target=retrier)
        thread.start()
        time.sleep(0.3)  # Many rounds in.

        def child() -> None:
            with Cache(directory, timeout=TIMEOUT) as mine:
                assert mine.get('before') == 'parent'
                mine.set('child', 1, retry=True)
                assert time.monotonic() >= released - 0.1, 'the child wrote while another process held the lock'

        start = time.monotonic()
        pid = fork(child)
        latency = time.monotonic() - start
        time.sleep(max(0.0, released - time.monotonic()))
        peer.rollback()
        thread.join(TIMEOUT + 1)
        code = wait(pid)

    if latency > 0.25:
        failures.append(f'fork took {latency:.3f}s while a write was retrying')
    if code != 0:
        failures.append(f'child exit code {code}')
    if thread.is_alive():
        failures.append('the retrying write never finished')
    if errors:
        failures.append(f'the retrying write failed: {errors[0]!r}')
    if cache.get('retried') != 1 or cache.get('child') != 1:
        failures.append(f'missing writes: retried={cache.get("retried")!r} child={cache.get("child")!r}')
    cache.close()
    return failures


def stats_after_fork(directory: Path) -> list[str]:
    """`stats()` counts per process (decision 19): a forked child starts at zero, keeps counting on,
    and the parent's counts are its own."""
    cache = Cache(directory, timeout=TIMEOUT)
    cache.stats(enable=True, reset=True)
    cache.set('k', 1)
    cache.get('k')

    def child() -> None:
        assert cache.stats() == (0, 0), cache.stats()
        cache.get('k')
        cache.get('missing')
        assert cache.stats() == (1, 1), cache.stats()

    code = wait(fork(child))
    failures = []
    if code != 0:
        failures.append(f'child exit code {code}')
    if cache.stats() != (1, 0):
        failures.append(f"the parent's counts changed: {cache.stats()}")
    cache.close()
    return failures


def rlock_after_fork(directory: Path) -> list[str]:
    """An RLock the parent holds is another owner's in a forked child (decision 18): the child can't
    re-enter it (it would wait) or release it (AssertionError), and the parent still can."""
    from diskcache_oxide import RLock

    cache = Cache(directory, timeout=TIMEOUT)
    rlock = RLock(cache, 'shared')
    rlock.acquire()

    def child() -> None:
        try:
            rlock.release()
        except AssertionError:
            pass
        else:
            raise AssertionError("the child released its parent's lock")
        got = threading.Event()
        threading.Thread(target=lambda: (rlock.acquire(), got.set()), daemon=True).start()
        assert not got.wait(0.3), "the child re-entered its parent's lock"

    code = wait(fork(child))
    failures = []
    if code != 0:
        failures.append(f'child exit code {code}')
    rlock.release()
    with rlock:
        pass
    cache.close()
    return failures


def two_forkers(directory: Path) -> list[str]:
    """Two threads fork at the same time, repeatedly, while a third reads and writes the cache."""
    rounds = 10
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    stop = threading.Event()
    barrier = threading.Barrier(2)
    codes: list[int | None] = []
    errors: list[BaseException] = []

    def hammer() -> None:
        i = 0
        try:
            while not stop.is_set():
                cache.set(f'h{i % 100}', i)
                cache.get('before')
                i += 1
        except BaseException as ex:
            errors.append(ex)

    def child(n: int, r: int) -> None:
        assert cache.get('before') == 'parent'
        assert cache.set(f'child-{n}-{r}', r) is True

    def forker(n: int) -> None:
        try:
            for r in range(rounds):
                barrier.wait(TIMEOUT)
                codes.append(wait(fork(lambda: child(n, r))))
        except BaseException as ex:
            errors.append(ex)

    threads = [
        threading.Thread(target=hammer),
        *(threading.Thread(target=forker, args=(n,)) for n in range(2)),
    ]
    for t in threads[1:]:
        t.start()
    threads[0].start()
    for t in threads[1:]:
        t.join(CHILD_DEADLINE * rounds)
    stop.set()
    threads[0].join(TIMEOUT)

    failures = [f'thread failed: {ex!r}' for ex in errors]
    if codes != [0] * (2 * rounds):
        failures.append(f'child exit codes {codes}')
    missing = [f'child-{n}-{r}' for n in range(2) for r in range(rounds) if cache.get(f'child-{n}-{r}') != r]
    if missing:
        failures.append(f'missing child writes: {missing}')
    cache.close()
    return failures


def parent_closes_after_fork(directory: Path) -> list[str]:
    """The parent forks with idle pooled connections, then closes, and an unrelated process's last
    close comes next: the child's later write must still reach the database.

    SQLite keeps one per-process lock record per database file. A connection left open across the
    fork hands the child that record, saying the process already holds SHARED, so the child's new
    connections take no kernel lock. With the parent closed, the unrelated process's last close then
    checkpoints and deletes the WAL under the child, and the child's write lands in the unlinked file.
    """
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()

    def child() -> None:
        os.close(ready_r)
        os.close(go_w)
        assert cache.get('before') == 'parent'
        os.write(ready_w, b'r')
        assert os.read(go_r, 1) == b'g'
        assert cache.set('child', 1) is True

    pid = fork(child)
    os.close(ready_w)
    os.close(go_r)
    failures = []
    if os.read(ready_r, 1) != b'r':
        failures.append('the child never got ready')
    cache.close()
    third = f'from diskcache_oxide import Cache; c = Cache({str(directory)!r}); c.set("third", 3); c.close()'
    subprocess.run([sys.executable, '-c', third], check=True, timeout=TIMEOUT * 2)
    os.write(go_w, b'g')
    code = wait(pid)
    if code != 0:
        failures.append(f'child exit code {code}')
    with Cache(directory, timeout=TIMEOUT) as fresh:
        if fresh.get('child') != 1:
            failures.append("the child's write is lost: a fresh Cache doesn't see it")
        if fresh.get('third') != 3:
            failures.append("the unrelated process's write is missing")
    return failures


def shared_iterator_while_a_fork_waits(directory: Path) -> list[str]:
    """Two threads call next() on one key iterator while a fork waits for a held transaction.

    The first next() waits for the pending fork with the interpreter released. The second must not
    block on the iterator while attached (holding the GIL, or stalling the fork's stop-the-world on
    a free-threaded build): nobody could then run Python to end the transaction, and the whole
    process hung. It raises ValueError at once instead, as a generator shared that way does.
    """
    cache = Cache(directory, timeout=TIMEOUT)
    for i in range(5):
        cache.set(f'k{i}', i)
    it = iter(cache)
    release = threading.Event()
    held = threading.Event()

    def holder() -> None:
        with cache.transact():
            held.set()
            release.wait(TIMEOUT)

    codes: list[int | None] = []
    threading.Thread(target=holder, daemon=True).start()
    held.wait(TIMEOUT)
    forker = threading.Thread(target=lambda: codes.append(wait(fork(lambda: None))), daemon=True)
    forker.start()
    time.sleep(0.2)  # The fork is now pending, waiting for the holder.

    got: list[object] = []
    first = threading.Thread(target=lambda: got.append(next(it)), daemon=True)
    first.start()
    time.sleep(0.2)  # Well into its wait for the fork, inside the iterator.
    failures = []
    try:
        second = next(it)
    except ValueError:
        pass
    else:
        failures.append(f'the second next() returned {second!r} instead of raising ValueError')
    release.set()
    first.join(TIMEOUT)
    forker.join(CHILD_DEADLINE)
    if first.is_alive() or forker.is_alive():
        failures.append('the first next() or the fork never finished')
    if got != ['k0']:
        failures.append(f'the first next() returned {got!r}')
    if codes != [0]:
        failures.append(f'child exit codes {codes}')
    if list(it) != ['k1', 'k2', 'k3', 'k4']:
        failures.append('the iterator lost its place')
    cache.close()
    return failures


def transact_fork_from_the_owner(directory: Path) -> list[str]:
    """Fork inside this thread's own `with cache.transact()` block (D-020, through the public API).

    In the child the inherited transaction is poisoned: a write inside it, a nested `transact()`
    (an inherited transaction doesn't count as this thread's, so it doesn't pass through), one on a
    fresh handle, and leaving the block all raise RuntimeError at once. The parent's block commits.
    """
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    txn = cache.transact()

    def child() -> None:
        fails_fast('write inside the inherited transaction', lambda: cache.set('child', 1))
        fails_fast('a nested transact()', lambda: cache.transact().__enter__())
        fails_fast("a fresh handle's transact()", lambda: Cache(directory, timeout=TIMEOUT).transact().__enter__())
        fails_fast('leaving the inherited transaction', lambda: txn.__exit__(None, None, None))
        assert cache.get('before') == 'parent'
        assert cache.get('in-tx') is None, "the parent's uncommitted write is visible"

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        with txn as tx:
            tx.set('in-tx', 1)
            code = wait(fork(child))
            tx.set('after-fork', 2)

    failures = []
    if code != 0:
        failures.append(f'child exit code {code}')
    if not any(issubclass(w.category, RuntimeWarning) for w in caught):
        failures.append('the parent got no RuntimeWarning for forking inside its transaction')
    if (cache.get('in-tx'), cache.get('after-fork')) != (1, 2):
        failures.append("the parent's transaction did not commit")
    if cache.get('child') is not None:
        failures.append('the poisoned child wrote to the database')
    cache.close()
    return failures


class ForkOnDecode:
    """A registered type whose decoder runs `ForkOnDecode.hook` (inside the read that decodes it)."""

    hook: Callable[[], None] | None = None

    def __init__(self, n: int) -> None:
        self.n = n


def fork_on_decode(n: int) -> ForkOnDecode:
    hook, ForkOnDecode.hook = ForkOnDecode.hook, None
    if hook is not None:
        hook()
    return ForkOnDecode(n)


def transact_inherited_mid_operation(directory: Path) -> list[str]:
    """Fork while another thread's held transaction has its connection lent to one of its reads.

    Thread B holds `cache.transact()` and reads a value whose decoder forks. The main thread's fork
    is already pending, waiting on B's level (a probe that times out on a short-timeout handle shows
    it), so B's before-fork hook parks behind it and the main thread forks first, with the held
    connection lent to B's read. In that child the transaction is inherited from a thread the child
    doesn't have: leaving it raises RuntimeError at once, and the database is unwritable (D-020).
    In the parent, B's own fork then goes ahead, and its transaction commits.
    """
    register_type(ForkOnDecode, name='tests.ForkOnDecode', encode=lambda v: v.n, decode=fork_on_decode)
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    cache.set('forker', ForkOnDecode(1))
    txn = cache.transact()
    inside = threading.Event()
    claimed = threading.Event()
    b_codes: list[int | None] = []
    errors: list[BaseException] = []

    def b_forks() -> None:
        inside.set()
        if not claimed.wait(TIMEOUT):
            raise AssertionError("the main thread's fork never became pending")
        b_codes.append(wait(fork(lambda: None)))  # Parks until the main thread has forked.

    def owner() -> None:
        try:
            with txn as tx:
                tx.set('in-tx', 1)
                ForkOnDecode.hook = b_forks
                got = tx.get('forker')
                assert isinstance(got, ForkOnDecode) and got.n == 1, got
                tx.set('after-fork', 2)
        except BaseException as ex:
            errors.append(ex)

    # Opened now: an open behind the pending fork would itself time out.
    short = Cache(directory, timeout=0.05)

    def probe() -> None:
        # No level of its own between calls: once a read times out, a fork is pending.
        end = time.monotonic() + TIMEOUT
        while time.monotonic() < end:
            try:
                short.get('before')
            except Timeout:
                claimed.set()
                return
            time.sleep(0.005)

    def child() -> None:
        fails_fast('leaving a transaction inherited mid-operation', lambda: txn.__exit__(None, None, None))
        assert cache.get('before') == 'parent'
        assert cache.get('in-tx') is None, "the parent's uncommitted write is visible"
        fails_fast('write through the inherited Cache', lambda: cache.set('child', 1))
        fails_fast('write through a fresh Cache', lambda: Cache(directory, timeout=TIMEOUT).set('child', 1))

    thread = threading.Thread(target=owner, daemon=True)
    thread.start()
    if not inside.wait(TIMEOUT):
        return ['the owner never reached the decoder']
    prober = threading.Thread(target=probe, daemon=True)
    prober.start()
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        code = wait(fork(child))
    thread.join(TIMEOUT * 2)
    prober.join(TIMEOUT)

    failures = [f'owner failed: {ex!r}' for ex in errors]
    if thread.is_alive():
        failures.append('the owner thread hung')
    if not claimed.is_set():
        failures.append('the probe never saw the fork pending')
    if code != 0:
        failures.append(f'child exit code {code}')
    if b_codes != [0]:
        failures.append(f"the owner's child exit codes {b_codes}")
    if (cache.get('in-tx'), cache.get('after-fork')) != (1, 2):
        failures.append("the owner's transaction did not commit")
    if cache.get('child') is not None:
        failures.append('the poisoned child wrote to the database')
    unregister_type(ForkOnDecode)
    short.close()
    cache.close()
    return failures


def transact_fork_from_another_thread(directory: Path) -> list[str]:
    """Fork while another thread is inside `with cache.transact()`.

    The fork waits for the transaction to end (its owner holds a gate level, so its own calls pass
    the pending fork), then the child sees everything it committed and can write.
    """
    cache = Cache(directory, timeout=TIMEOUT)
    inside = threading.Event()

    def owner() -> None:
        with cache.transact() as tx:
            tx.set('in-tx', 1)
            inside.set()
            time.sleep(0.3)  # The fork is pending meanwhile.
            tx.set('late', 2)

    def child() -> None:
        assert (cache.get('in-tx'), cache.get('late')) == (1, 2), 'the fork did not wait for the commit'
        assert cache.set('child', 3) is True
        with cache.transact() as tx:
            tx.set('child-tx', 4)
        assert cache.get('child-tx') == 4

    thread = threading.Thread(target=owner, daemon=True)
    thread.start()
    failures = []
    if not inside.wait(TIMEOUT):
        return ['the owner never entered its transaction']
    started = time.monotonic()
    pid = fork(child)
    took = time.monotonic() - started
    code = wait(pid)
    thread.join(TIMEOUT)
    if code != 0:
        failures.append(f'child exit code {code}')
    if took < 0.2:
        failures.append(f'the fork returned after {took:.2f}s, before the transaction ended')
    if (cache.get('child'), cache.get('child-tx')) != (3, 4):
        failures.append("the child's writes are missing")
    cache.close()
    return failures


def fanout_fork_while_writing(directory: Path) -> list[str]:
    """Fork while threads write to different shards of an 8-store FanoutCache (plan 13, step 13d).

    The gate, the registry and the generations are process-wide, so eight stores need no new
    protocol, only proof: the child writes and reads back a key on every shard, through the
    inherited handle and through a fresh one, and the parent then sees every child write.
    """
    rounds = 5
    cache = FanoutCache(directory, timeout=TIMEOUT)
    # One key per shard, so each writer's `set` and each child write hits a different store (a
    # writer's `{key}-count` counter lands wherever it hashes).
    per_shard: dict[int, str] = {}
    i = 0
    while len(per_shard) < cache.shards:
        per_shard.setdefault(cache.shard_of(f'k{i}'), f'k{i}')
        i += 1
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer(key: str) -> None:
        n = 0
        try:
            while not stop.is_set():
                cache.set(key, n)
                cache.incr(f'{key}-count')
                n += 1
        except BaseException as ex:
            errors.append(ex)

    def child(r: int) -> None:
        for shard, key in per_shard.items():
            name = f'child-{r}-{key}'
            assert cache.set(name, r) is True, f'shard {shard}'
            assert cache.get(name) == r
        with FanoutCache(directory, timeout=TIMEOUT) as fresh:
            assert fresh.shards == cache.shards
            for key in per_shard.values():
                assert fresh.get(f'child-{r}-{key}') == r
                assert fresh.set(f'fresh-{r}-{key}', r) is True

    threads = [threading.Thread(target=writer, args=(key,)) for key in per_shard.values()]
    for t in threads:
        t.start()
    codes = [wait(fork(lambda: child(r))) for r in range(rounds)]
    stop.set()
    for t in threads:
        t.join(TIMEOUT)

    failures = [f'writer failed: {ex!r}' for ex in errors]
    if codes != [0] * rounds:
        failures.append(f'child exit codes {codes}')
    missing = [
        f'{prefix}-{r}-{key}'
        for r in range(rounds)
        for key in per_shard.values()
        for prefix in ('child', 'fresh')
        if cache.get(f'{prefix}-{r}-{key}') != r
    ]
    if missing:
        failures.append(f'missing child writes: {missing}')
    cache.close()
    if cache._handle_counts() != [0] * 8:
        failures.append(f'handles left after close: {cache._handle_counts()}')
    return failures


def one_key_per_shard(cache: FanoutCache, prefix: str) -> dict[int, str]:
    """A key that routes to each shard, so a loop over them touches every store."""
    keys: dict[int, str] = {}
    i = 0
    while len(keys) < cache.shards:
        keys.setdefault(cache.shard_of(f'{prefix}{i}'), f'{prefix}{i}')
        i += 1
    return keys


def fanout_fork_mid_merge(directory: Path) -> list[str]:
    """Fork in the middle of an `iterkeys()` merge over 8 shards (plan 13, step 13g).

    The main thread has taken 300 keys from a merge, so every shard's page buffer is filled and its
    cursor is part way, and it forks while another thread writes and a third runs merges of its own.
    A merge holds no connection between yields (D-025), so the child carries on the inherited one from
    where the parent stopped, sorted and with nothing repeated or lost, and so does the parent after
    the forks. A fresh merge in the child, through the inherited handle and a fresh one, sees every
    key, and the child's own writes on every shard come back merged, in both directions.
    """
    rounds = 4
    taken = 300
    cache = FanoutCache(directory, timeout=TIMEOUT)
    keys = [f'k{i:04d}' for i in range(2_000)]
    for key in keys:
        cache.set(key, key)
    expected = sorted(keys)
    it = cache.iterkeys()
    head = [next(it) for _ in range(taken)]
    stop = threading.Event()
    errors: list[str] = []

    def writer() -> None:
        n = 0
        try:
            while not stop.is_set():
                cache.set(f'w{n % 500:03d}', n)  # 'w' sorts after every 'k' key.
                n += 1
        except BaseException as ex:
            errors.append(f'writer failed: {ex!r}')

    def merger() -> None:
        try:
            while not stop.is_set():
                merged = str_keys(cache.iterkeys())
                if any(a >= b for a, b in zip(merged, merged[1:], strict=False)):
                    errors.append('a merge during the forks was out of order or repeated a key')
                if [k for k in merged if k.startswith('k')] != expected:
                    errors.append('a merge during the forks lost a key')
        except BaseException as ex:
            errors.append(f'merger failed: {ex!r}')

    def check_rest(rest: list[str]) -> None:
        assert rest[: len(expected) - taken] == expected[taken:], 'the merge lost its place'
        tail = rest[len(expected) - taken :]
        assert all(k.startswith('w') for k in tail), f'unexpected keys after the k range: {tail[:5]}'
        assert all(a < b for a, b in zip(tail, tail[1:], strict=False)), 'the tail is out of order'

    def child(r: int) -> None:
        check_rest(str_keys(it))
        assert list(cache.iterkeys(prefix='k')) == expected
        mine = sorted(one_key_per_shard(cache, f'child-{r}-').values())
        for key in mine:
            assert cache.set(key, r) is True
        assert list(cache.iterkeys(prefix=f'child-{r}-')) == mine
        assert list(cache.iterkeys(prefix=f'child-{r}-', reverse=True)) == mine[::-1]
        with FanoutCache(directory, timeout=TIMEOUT) as fresh:
            assert list(fresh.iterkeys(prefix='k')) == expected

    threads = [threading.Thread(target=writer), threading.Thread(target=merger)]
    for t in threads:
        t.start()
    time.sleep(0.1)
    codes = [wait(fork(lambda: child(r))) for r in range(rounds)]
    stop.set()
    for t in threads:
        t.join(TIMEOUT * 2)

    failures = list(dict.fromkeys(errors))
    if head != expected[:taken]:
        failures.append('the first keys of the merge are wrong')
    try:
        check_rest(str_keys(it))
    except AssertionError as ex:
        failures.append(f'in the parent: {ex}')
    if codes != [0] * rounds:
        failures.append(f'child exit codes {codes}')
    for r in range(rounds):
        if len(list(cache.iterkeys(prefix=f'child-{r}-'))) != cache.shards:
            failures.append(f"child {r}'s writes are missing")
    cache.close()
    return failures


def fanout_close_after_fork(directory: Path) -> list[str]:
    """Close after a fork, on 8 shards: D-018 across shards, and the lock-record trap on each of them.

    The parent forks while threads write to every shard, waits until the child has read every shard,
    and closes: no fd stays open inside the root. An unrelated process then opens and closes the
    FanoutCache, which checkpoints and deletes each shard's WAL, and only then does the child write to
    every shard: each write must reach its database, as `parent-closes-after-fork` checks for one
    store. The child inherits exactly one pin per shard and no connection (the forking thread held
    none), and its close leaves just those pins, forgotten by design (D-017), never a connection it
    opened itself.
    """
    cache = FanoutCache(directory, timeout=TIMEOUT)
    shards = cache.shards
    keys = one_key_per_shard(cache, 'k')
    for key in keys.values():
        cache.set(key, 'parent')
    ready_r, ready_w = os.pipe()
    go_r, go_w = os.pipe()
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer(key: str) -> None:
        try:
            while not stop.is_set():
                cache.set(f'{key}-busy', 1)
        except BaseException as ex:
            errors.append(ex)

    def child() -> None:
        os.close(ready_r)
        os.close(go_w)
        inherited = fds_inside(directory)
        # One pin per shard; the forking thread held no connection, so the child inherits no other fd.
        assert inherited == shards, f'the child inherited {inherited} fds inside the root, not one pin per shard'
        for key in keys.values():
            assert cache.get(key) == 'parent'
        os.write(ready_w, b'r')
        assert os.read(go_r, 1) == b'g'
        for key in keys.values():
            assert cache.set(f'{key}-child', 1) is True
        cache.close()
        left = fds_inside(directory)
        assert left == inherited, f'the child left {left} fds open after close(), and inherited {inherited} pins'

    writers = [threading.Thread(target=writer, args=(key,)) for key in keys.values()]
    for t in writers:
        t.start()
    time.sleep(0.1)
    pid = fork(child)
    stop.set()
    for t in writers:
        t.join(TIMEOUT)
    os.close(ready_w)
    os.close(go_r)
    failures = [f'writer failed: {ex!r}' for ex in errors]
    if os.read(ready_r, 1) != b'r':
        failures.append('the child never got ready')
    cache.close()
    if (left := fds_inside(directory)) != 0:
        failures.append(f'D-018: {left} fds still open inside the root after the parent closed')
    if cache._handle_counts() != [0] * shards:
        failures.append(f'handles left after close: {cache._handle_counts()}')
    third = (
        f'from diskcache_oxide import FanoutCache; c = FanoutCache({str(directory)!r}); c.set("third", 3); c.close()'
    )
    subprocess.run([sys.executable, '-c', third], check=True, timeout=TIMEOUT * 2)
    os.write(go_w, b'g')
    code = wait(pid)
    if code != 0:
        failures.append(f'child exit code {code}')
    with FanoutCache(directory, timeout=TIMEOUT) as fresh:
        lost = [shard for shard, key in keys.items() if fresh.get(f'{key}-child') != 1]
        if lost:
            failures.append(f"the child's writes are lost on shards {lost}: a fresh handle doesn't see them")
        if fresh.get('third') != 3:
            failures.append("the unrelated process's write is missing")
    if (left := fds_inside(directory)) != 0:
        failures.append(f'{left} fds open inside the root after the last close')
    return failures


def fanout_fork_mid_clear(directory: Path) -> list[str]:
    """Fork while another thread's `clear()` is part way through 8 shards.

    Another process holds shard 5's write lock, so `clear()` has emptied shards 0 to 4 and waits on 5
    when the fork lands. The fork doesn't wait for it (the wait is outside the fork gate), and the
    child reads and writes every shard, its write on shard 5 waiting for the other process. `clear()`
    took every shard's snapshot before it started (plan 13, step 13e), so in the parent it removes
    exactly the keys that existed then, and the child's writes all survive, on the shards it had
    already emptied and on those it hadn't reached (D-011, across processes).
    """
    stalled_on = 5
    cache = FanoutCache(directory, timeout=TIMEOUT)
    shards = cache.shards
    per_shard = [0] * shards
    for i in range(2_000 * shards):
        cache.set(f'old{i:06d}', i)
        per_shard[cache.shard_of(f'old{i:06d}')] += 1
    filled = sum(per_shard)
    unreached = sum(per_shard[stalled_on:])
    removed: list[int] = []
    errors: list[BaseException] = []

    def clearing() -> None:
        try:
            removed.append(cache.clear())
        except BaseException as ex:
            errors.append(ex)

    def child() -> None:
        mine = one_key_per_shard(cache, 'child-')
        for key in mine.values():
            assert cache.set(key, 'child') is True
            assert cache.get(key) == 'child'
        assert len(list(cache.iterkeys(prefix='child-'))) == shards

    failures: list[str] = []
    with SqlitePeer(directory / f'{stalled_on:03d}' / 'diskcache-oxide.sqlite') as peer:
        peer.begin('IMMEDIATE')
        thread = threading.Thread(target=clearing)
        thread.start()
        deadline = time.monotonic() + TIMEOUT
        while len(cache) != unreached and time.monotonic() < deadline:
            time.sleep(0.005)
        left_at_fork = len(cache)
        started = time.monotonic()
        pid = fork(child)
        latency = time.monotonic() - started
        time.sleep(0.2)  # The child is now waiting for shard 5, too.
        peer.rollback()
        code = wait(pid)
        thread.join(CHILD_DEADLINE)

    failures += [f'clear() failed: {ex!r}' for ex in errors]
    if left_at_fork != unreached:
        failures.append(f'the fork did not land with clear() on shard {stalled_on} ({left_at_fork} keys left)')
    if latency > 0.25:
        failures.append(f'fork took {latency:.3f}s while clear() waited for a shard')
    if code != 0:
        failures.append(f'child exit code {code}')
    if removed != [filled]:
        failures.append(f'clear() removed {removed}, expected exactly the {filled} keys it started with')
    survivors = sorted(str_keys(cache.iterkeys()))
    if len(survivors) != shards or not all(k.startswith('child-') for k in survivors):
        failures.append(f"after clear(), expected the child's {shards} keys, found {survivors[:10]}")
    cache.close()
    return failures


def registry_after_fork(directory: Path) -> list[str]:
    """Fork while 16 threads encode registered values (plan 16): the child reads a registered value
    through the inherited snapshot and registers a new type under a deadline. The registry's reads
    are lock-free and its writes retry a compare-and-swap, so neither can inherit a lock a dead
    thread held; this is the proof, since the plan takes arc-swap's fork behavior on trust."""
    import dataclasses

    import diskcache_oxide

    @dataclasses.dataclass
    class Point:
        x: int
        y: int = 0

    class Later:
        def __init__(self, v: int) -> None:
            self.v = v

    diskcache_oxide.register_type(Point, name='fork.Point')
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('p', Point(1, 2))
    stop = threading.Event()
    errors: list[BaseException] = []

    def encoder(i: int) -> None:
        n = 0
        try:
            while not stop.is_set():
                cache.set(f'k{i}', {'p': Point(n, i), 'list': [Point(n)] * 3})
                n += 1
        except BaseException as ex:
            errors.append(ex)

    def child() -> None:
        assert cache.get('p') == Point(1, 2)
        start = time.monotonic()
        diskcache_oxide.register_type(Later, name='fork.Later', encode=lambda o: o.v, decode=Later)
        assert time.monotonic() - start < 2.0, 'registering in the child stalled'
        cache.set('later', Later(7))
        assert cache.get('later').v == 7
        assert cache.set('q', Point(3)) is True
        assert cache.get('q') == Point(3)

    threads = [threading.Thread(target=encoder, args=(i,)) for i in range(16)]
    for t in threads:
        t.start()
    time.sleep(0.2)
    codes = [wait(fork(child)) for _ in range(3)]
    stop.set()
    for t in threads:
        t.join(TIMEOUT)
    failures = [f'encoder failed: {ex!r}' for ex in errors]
    if codes != [0, 0, 0]:
        failures.append(f'child exit codes {codes}')
    if cache.get('q') != Point(3):
        failures.append('the parent cannot read what the child wrote')
    if 'fork.Later' in diskcache_oxide.registered_types():
        failures.append("the child's registration leaked into the parent")
    cache.close()
    return failures


def d020_wal(directory: Path) -> list[str]:
    """Fork inside a held transaction whose writes have already reached the WAL (D-020).

    The parent writes several times its connection's page cache inside the hold, so SQLite has
    spilled most of the transaction's pages to the WAL before the fork. The child inherits that
    connection and leaves the transaction; it must only forget the connection, never close it: a
    close would roll the transaction back through the shared WAL index, under the parent. The
    parent then reads every value back inside the hold (from the WAL, since they no longer fit in
    its page cache), again after its commit, and a fresh process reads them once more.
    """
    cache = Cache(directory, timeout=TIMEOUT)
    cache.set('before', 'parent')
    wal = directory / 'diskcache-oxide.sqlite-wal'
    values = {f'k{i:03d}': bytes([i % 251]) * 40_000 for i in range(200)}  # 8 MB inline: 8 page caches.
    hold = cache.transact()
    hold.__enter__()
    for key, value in values.items():
        cache.set(key, value)
    spilled = wal.stat().st_size

    def child() -> None:
        fails_fast('leaving the inherited transaction', lambda: hold.__exit__(None, None, None))
        assert cache.get('before') == 'parent', 'a read after leaving the transaction'
        assert cache.get('k000') is None, "the parent's uncommitted write is visible"

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', RuntimeWarning)
        pid = fork(child)
    code = wait(pid)

    failures = []
    if code != 0:
        failures.append(f'child exit code {code}')
    if spilled < 4 * 2**20:
        failures.append(f'only {spilled} bytes were in the WAL before the fork, so this proves nothing')
    wrong = [key for key, value in values.items() if cache.get(key) != value]
    if wrong:
        failures.append(f'inside the hold after the fork, {len(wrong)} of {len(values)} values read back wrong')
    hold.__exit__(None, None, None)
    wrong = [key for key, value in values.items() if cache.get(key) != value]
    if wrong:
        failures.append(f'after the commit, {len(wrong)} of {len(values)} values read back wrong')
    cache.close()
    script = (
        'import sys\n'
        'from diskcache_oxide import Cache\n'
        'cache = Cache(sys.argv[1])\n'
        'bad = [i for i in range(200) if cache.get(f"k{i:03d}") != bytes([i % 251]) * 40_000]\n'
        'print(len(bad), len(cache))\n'
    )
    fresh = subprocess.run([sys.executable, '-c', script, str(directory)], capture_output=True, text=True, timeout=60)
    if fresh.stdout.split() != ['0', str(len(values) + 1)]:
        failures.append(
            f'a fresh process read back (wrong, len) = {fresh.stdout.strip()!r} {fresh.stderr.strip()[-200:]}'
        )
    return failures


SCENARIOS = {
    'd020': d020,
    'd020-wal': d020_wal,
    'registry-after-fork': registry_after_fork,
    'transact-fork-from-the-owner': transact_fork_from_the_owner,
    'transact-fork-from-another-thread': transact_fork_from_another_thread,
    'transact-inherited-mid-operation': transact_inherited_mid_operation,
    'fork-while-a-write-waits': fork_while_a_write_waits,
    'fork-while-a-write-retries': fork_while_a_write_retries,
    'two-forkers': two_forkers,
    'stats-after-fork': stats_after_fork,
    'rlock-after-fork': rlock_after_fork,
    'parent-closes-after-fork': parent_closes_after_fork,
    'shared-iterator-while-a-fork-waits': shared_iterator_while_a_fork_waits,
    'fanout-fork-while-writing': fanout_fork_while_writing,
    'fanout-fork-mid-merge': fanout_fork_mid_merge,
    'fanout-close-after-fork': fanout_close_after_fork,
    'fanout-fork-mid-clear': fanout_fork_mid_clear,
}


def main() -> int:
    name, directory = sys.argv[1], Path(sys.argv[2])
    set_title(f'fork-{name}')
    # Forking with threads running is the point of these scenarios.
    warnings.filterwarnings('ignore', r'This process .* is multi-threaded', DeprecationWarning)
    failures = SCENARIOS[name](directory)
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
