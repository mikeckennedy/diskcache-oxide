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

from diskcache_oxide import Cache
from tests.conformance.proc_workers import SqlitePeer
from tests.proctitle import set_title

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
    hold = cache._hold_write_transaction()
    hold.__enter__()
    cache.set('in-hold', 1)

    def grandchild() -> None:
        fails_fast('grandchild write, fresh Cache', lambda: Cache(directory, timeout=TIMEOUT).set('grandchild', 1))
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

    threads = [threading.Thread(target=hammer), *(threading.Thread(target=forker, args=(n,)) for n in range(2))]
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
        with cache._hold_write_transaction():
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


SCENARIOS = {
    'd020': d020,
    'fork-while-a-write-waits': fork_while_a_write_waits,
    'two-forkers': two_forkers,
    'parent-closes-after-fork': parent_closes_after_fork,
    'shared-iterator-while-a-fork-waits': shared_iterator_while_a_fork_waits,
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
