"""Concurrency rules from 04-design.md section 10 that the conformance suite can't reach.

Fork scenarios run in a fresh process (tests/unit/fork_scenarios.py), never in pytest itself.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from diskcache_oxide import Cache, Timeout
from tests.conformance.proc_workers import SqlitePeer

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    'scenario',
    [
        'd020',
        'fork-while-a-write-waits',
        'two-forkers',
        'parent-closes-after-fork',
        'shared-iterator-while-a-fork-waits',
    ],
)
def test_fork_scenario(scenario: str, tmp_path: Path) -> None:
    proc = subprocess.run(
        [sys.executable, '-m', 'tests.unit.fork_scenarios', scenario, str(tmp_path / 'c')],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# clear(): same-process fairness, and writes made while it runs survive (D-011)
# ---------------------------------------------------------------------------

CLEAR_KEYS = 50_000
CLEAR_ATTEMPTS = 3


def _writes_during_clear(cache: Cache, count: int) -> tuple[int, int, list[str]] | None:
    """Clear `count` keys on another thread while this one writes with the cache's 1 s timeout.

    Returns (removed, writes landed while clear() ran, the keys written), or None if clear()
    finished before a write could land. Every write must succeed: a same-process writer queues for
    the in-process write lock, and clear() yields to it between chunks.
    """
    removed: list[int] = []
    clearing = threading.Thread(target=lambda: removed.append(cache.clear()))
    clearing.start()
    deadline = time.monotonic() + 20
    while len(cache) == count and clearing.is_alive():
        assert time.monotonic() < deadline, 'clear() never started'
    written: list[str] = []
    landed = 0
    while clearing.is_alive():
        # Alternate new keys and overwrites of keys clear() may not have reached yet.
        key = f'new{len(written)}' if len(written) % 2 else f'k{count - 1 - len(written)}'
        assert cache.set(key, 'during') is True
        written.append(key)
        landed += clearing.is_alive()
    clearing.join()
    return (removed[0], landed, written) if landed else None


def test_clear_lets_same_process_writers_in_and_spares_their_writes(tmp_path: Path) -> None:
    _clear_fairness(tmp_path)


@pytest.mark.slow
@pytest.mark.timeout(600)
@pytest.mark.parametrize('run', range(20))
def test_clear_lets_same_process_writers_in_and_spares_their_writes_20x(tmp_path: Path, run: int) -> None:
    _clear_fairness(tmp_path)


def _clear_fairness(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=1.0)
    count = CLEAR_KEYS
    for _ in range(CLEAR_ATTEMPTS):
        cache.clear()
        for i in range(count):
            cache.set(f'k{i}', i)
        try:
            outcome = _writes_during_clear(cache, count)
        except Timeout:
            pytest.fail('a same-process write timed out during clear()')
        if outcome is not None:
            break
        count *= 2
    else:
        pytest.fail(f'clear() finished before any write could land, even with {count // 2} keys')

    removed, landed, written = outcome
    assert landed >= 1
    # Only rows stored before clear() started are removed; an overwrite during it is a new store.
    overwrites = sum(1 for key in written if key.startswith('k'))
    assert count - overwrites <= removed <= count
    survivors = set(cache.iterkeys())
    assert survivors == set(written), f'{len(set(written) - survivors)} writes made during clear() were lost'
    assert all(cache.get(key) == 'during' for key in written)
    cache.close()


def test_cull_finishes_promptly_while_threads_hammer_writes(tmp_path: Path) -> None:
    # cull()'s eviction loop doesn't yield between chunks the way clear() does: measured on 3.14t,
    # yielding made it 3.5x slower against 4 writer threads (2.6 s vs 0.73 s for 98k rows) and only
    # trimmed the writers' worst wait from about 24 to 15 ms. The write lock's starvation mode
    # bounds both sides at about STARVE_AFTER (20 ms) instead. This guards cull()'s side: it must
    # never be starved out by tight set() loops (the clear() bug on free-threaded Linux).
    cache = Cache(tmp_path / 'c', timeout=30.0, size_limit=2**20, cull_limit=0)
    for i in range(30_000):
        cache.set(f'k{i}', 'x' * 400)
    stop = threading.Event()

    def hammer(n: int) -> None:
        i = 0
        while not stop.is_set():
            cache.set(f'w{n}-{i % 50}', i)
            i += 1

    writers = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in writers:
        t.start()
    try:
        time.sleep(0.1)
        started = time.perf_counter()
        assert cache.cull() > 25_000
        took = time.perf_counter() - started
    finally:
        stop.set()
        for t in writers:
            t.join()
        cache.close()
    # About 0.25 s here; 300 chunks starved for 20 ms each would take 6 s.
    assert took < 4, f'cull() took {took:.2f} s against 4 writer threads'


# ---------------------------------------------------------------------------
# Detached busy retry: waiting for another process's lock doesn't hold the interpreter
# ---------------------------------------------------------------------------


def test_waiting_for_another_processs_lock_lets_other_threads_run(tmp_path: Path) -> None:
    cache = Cache(tmp_path / 'c', timeout=5.0)
    cache.set('k', 'v')
    gaps: list[float] = []
    stop = threading.Event()

    def ticker() -> None:
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.002)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    with SqlitePeer(tmp_path / 'c' / 'diskcache-oxide.sqlite') as peer:
        peer.begin('IMMEDIATE')
        writer = threading.Thread(target=lambda: cache.set('k', 'w'))
        writer.start()
        time.sleep(0.1)  # The write is now waiting for the peer.
        tick = threading.Thread(target=ticker)
        tick.start()
        time.sleep(0.6)
        assert writer.is_alive(), 'the write should still be waiting for the peer'
        peer.rollback()
        writer.join(5)
        stop.set()
        tick.join()

    assert cache.get('k') == 'w'
    # With the interpreter held for the whole wait, the ticker would stall for about 0.6 s.
    assert max(gaps) < 0.1, f'another thread stalled for {max(gaps):.3f}s while a write waited'
    cache.close()


@pytest.mark.skipif(not sys._is_gil_enabled(), reason='the GIL hand-off cost exists only on GIL builds')
def test_clear_does_not_re_take_the_gil_between_chunks(tmp_path: Path) -> None:
    # clear() runs detached. Its waits between chunks used to re-attach the interpreter only to
    # release it again, and with other threads busy each re-attach waited for the GIL, up to the
    # 5 ms switch interval: about 15 ms a chunk with two reader threads (bench.reattach), so about
    # 0.75 s for these 50 chunks. Without the re-attach it takes about 15 ms.
    cache = Cache(tmp_path / 'c', size_limit=2**34)
    for i in range(5_000):
        cache.set(f'k{i}', i)
    stop = threading.Event()

    def read() -> None:
        while not stop.is_set():
            cache.get('k1')

    readers = [threading.Thread(target=read) for _ in range(2)]
    for t in readers:
        t.start()
    try:
        time.sleep(0.05)
        started = time.perf_counter()
        assert cache.clear() == 5_000
        took = time.perf_counter() - started
    finally:
        stop.set()
        for t in readers:
            t.join()
        cache.close()
    assert took < 0.3, f'clear() took {took:.2f} s: it waits for the GIL between chunks again'


# ---------------------------------------------------------------------------
# Held transactions: Python code running inside one of the transaction's own operations
# ---------------------------------------------------------------------------

# Run in a fresh process: the hang this guards against held the GIL, so a thread with a timeout
# couldn't catch it.
PICKLE_HOOK_INSIDE_A_HOLD = """
import sys
from diskcache_oxide import Cache

class Lazy:
    def __init__(self, n):
        self.n = n

    def __setstate__(self, state):
        self.__dict__.update(state)
        self.sibling = CACHE.get('plain')  # Touches the cache while it's being rebuilt.

CACHE = Cache(sys.argv[1], pickle=True)
CACHE.set('plain', 1)
CACHE.set('lazy', Lazy(7))
with CACHE._hold_write_transaction():
    CACHE.set('in-hold', 2)
    value = CACHE.get('lazy')
    assert (value.n, value.sibling) == (7, 1), vars(value)
assert CACHE.get('in-hold') == 2
"""


def test_a_pickle_hook_that_reads_the_cache_works_inside_a_held_transaction(tmp_path: Path) -> None:
    # The unpickling ran while the transaction's connection was locked, and the hook's get locked
    # it again on the same thread: a self-deadlock with the write lock held.
    proc = subprocess.run(
        [sys.executable, '-c', PICKLE_HOOK_INSIDE_A_HOLD, str(tmp_path / 'c')],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


ENDED_FROM_ANOTHER_THREAD_MID_OPERATION = """
import sys
import threading
import time
from diskcache_oxide import Cache

inside = threading.Event()

class Slow:
    def __init__(self):
        self.n = 1

    def __setstate__(self, state):
        self.__dict__.update(state)
        inside.set()
        time.sleep(0.3)  # Releases the interpreter; the other thread ends the transaction meanwhile.

CACHE = Cache(sys.argv[1], pickle=True)
CACHE.set('slow', Slow())
hold = CACHE._hold_write_transaction()
hold.__enter__()
CACHE.set('in-hold', 1)

def end() -> None:
    inside.wait()
    hold.__exit__(None, None, None)

ender = threading.Thread(target=end)
ender.start()
assert CACHE.get('slow').n == 1
ender.join()
assert CACHE.get('in-hold') == 1, 'the transaction ended on the other thread did not commit'
"""


def test_a_held_transaction_ended_from_another_thread_mid_operation_waits_detached(tmp_path: Path) -> None:
    # Ending it waits for the operation running on the transaction's connection. That wait held
    # the interpreter, so on a GIL build the operation could never finish: a deadlock.
    proc = subprocess.run(
        [sys.executable, '-c', ENDED_FROM_ANOTHER_THREAD_MID_OPERATION, str(tmp_path / 'c')],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# Another library's after-fork hook that runs before ours and uses the cache
# ---------------------------------------------------------------------------

# A fresh process: the hook must be registered before diskcache_oxide is imported, since CPython
# runs after_in_child hooks in registration order.
EARLY_CHILD_HOOK = """
import os
import sys
import warnings

outcome = []

def early_hook():
    try:
        CACHE.set('from-early-hook', 1)
        outcome.append('wrote')
    except RuntimeError:
        outcome.append('refused')

os.register_at_fork(after_in_child=early_hook)

from diskcache_oxide import Cache

CACHE = Cache(sys.argv[1])
CACHE.set('before', 1)
hold = CACHE._hold_write_transaction()
hold.__enter__()
with warnings.catch_warnings():
    warnings.simplefilter('ignore', RuntimeWarning)
    pid = os.fork()
if pid == 0:
    os._exit(0 if outcome == ['refused'] else 1)
_, status = os.waitpid(pid, 0)
hold.__exit__(None, None, None)
assert os.waitstatus_to_exitcode(status) == 0, 'the early hook wrote inside the inherited transaction'
assert CACHE.get('from-early-hook') is None
assert CACHE.get('before') == 1
"""


def test_an_earlier_after_fork_hook_sees_the_child_side_reset(tmp_path: Path) -> None:
    # The child-side reset ran in our own after_in_child hook, so a hook registered earlier saw the
    # parent's state: here, the transaction the parent forked inside, not yet poisoned, which the
    # hook's write went straight into.
    proc = subprocess.run(
        [sys.executable, '-c', EARLY_CHILD_HOOK, str(tmp_path / 'c')],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# The store's own descriptor on the database never drops SQLite's locks
# ---------------------------------------------------------------------------

# Run in another process: POSIX locks are per process, so this one can't see its own. SQLite's
# SHARED lock is a read lock on 510 bytes from offset 0x40000002 (its default PENDING_BYTE + 2).
SHARED_LOCK_PROBE = """
import fcntl, os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
try:
    fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 510, 0x40000002, 0)
except OSError:
    sys.exit(0)  # Someone holds SHARED.
sys.exit(1)
"""


def _another_process_sees_shared_held(db: Path) -> bool:
    return subprocess.run([sys.executable, '-c', SHARED_LOCK_PROBE, str(db)], timeout=30).returncode == 0


def test_reusing_a_closed_handle_keeps_this_processs_sqlite_locks(tmp_path: Path) -> None:
    # Rejoining the store opened a plain file descriptor on the database and closed it again, and
    # closing any descriptor on a file drops every POSIX lock the process holds on it: here the
    # SHARED lock a WAL connection keeps, which stops another process's last close from
    # checkpointing and deleting the WAL under this one.
    first = Cache(tmp_path / 'c')
    second = Cache(tmp_path / 'c')
    second.set('k', 1)
    db = tmp_path / 'c' / 'diskcache-oxide.sqlite'
    assert _another_process_sees_shared_held(db)
    first.close()
    assert first.get('k') == 1  # The closed handle rejoins the live store.
    assert _another_process_sees_shared_held(db), "this process's SQLite locks were dropped"
    first.close()
    second.close()
