"""Module-level process targets for test_processes.py, plus a small CLI.

Spawned children import this module by name, so it must stay importable from the repo root and
must not import pytest fixtures. The CLI runs in a fresh interpreter (cwd = repo root):

    python -m tests.conformance.proc_workers fork <backend> <directory> <mode>
        The fork checks (see FORK_MODES and MULTI_CACHE_MODES), isolated so macOS warnings about forking a process
        that has threads can't touch the pytest session. Exit code 0 on success; failures print
        to stderr.

    python -m tests.conformance.proc_workers peer <db>
        A second SQLite connection on <db>, driven one command per stdin line, one reply line each:
        `begin <IMMEDIATE|EXCLUSIVE|READ>` -> `locked` (READ holds a read snapshot), `rollback` -> `released`,
        `data_version` -> the integer. EOF closes the connection.

    python -m tests.conformance.proc_workers integrity <db>
        Prints the result of PRAGMA integrity_check.

The stdlib sqlite3 module is only ever imported by the peer and integrity commands, never in a
process that has the cache under test open. Two copies of SQLite in one process (stdlib vs
oxide's bundled rusqlite) don't share POSIX lock state, and closing one copy's file descriptor
silently drops the other's locks.
"""

from __future__ import annotations

import hashlib
import multiprocessing
import os
import random
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.proctitle import set_title, titled

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier, Event

    from tests.conformance.backends import Backend

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_TIMEOUT = 30.0
# The floor for spill_min(); every spilled value goes through spill_min(backend), never this.
_SPILL_FLOOR = 100_000
# make_blob() sizes are uniform in [min_size, min_size + SPILL_SPREAD).
SPILL_SPREAD = 300_000
DIGEST = 32


def _backend(name: str) -> Backend:
    # Imported lazily so the peer and integrity commands start fast and never load a cache backend.
    from tests.conformance.backends import BACKENDS

    return BACKENDS[name]()


def spill_min(backend: Backend) -> int:
    """The smallest blob size: at least 100 KB, and always at least twice the spill threshold."""
    return max(_SPILL_FLOOR, 2 * backend.spill_threshold)


def integrity(db_path: Path) -> str:
    """PRAGMA integrity_check, run in a helper subprocess (see the module docstring)."""
    proc = subprocess.run(
        [
            sys.executable,
            '-m',
            'tests.conformance.proc_workers',
            'integrity',
            str(db_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=SYNC_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'integrity helper failed: {proc.stderr}')
    return proc.stdout.strip()


class SqlitePeer:
    """Client for the `peer` command: a second SQLite connection living in its own process."""

    def __init__(self, db_path: Path) -> None:
        assert Path(db_path).is_file(), 'the cache has not created its database yet'
        self._proc = subprocess.Popen(
            [
                sys.executable,
                '-m',
                'tests.conformance.proc_workers',
                'peer',
                str(db_path),
            ],
            cwd=REPO_ROOT,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def _ask(self, command: str) -> str:
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write(command + '\n')
        self._proc.stdin.flush()
        reply = self._proc.stdout.readline().strip()
        if not reply or reply.startswith('error'):
            self.close()
            raise RuntimeError(
                f'peer {command!r} failed: {reply} {self._proc.stderr.read() if self._proc.stderr else ""}'
            )
        return reply

    def begin(self, mode: str) -> None:
        assert self._ask(f'begin {mode}') == 'locked'

    def rollback(self) -> None:
        assert self._ask('rollback') == 'released'

    def data_version(self) -> int:
        return int(self._ask('data_version'))

    def close(self) -> None:
        if self._proc.stdin and not self._proc.stdin.closed:
            self._proc.stdin.close()
        try:
            self._proc.wait(SYNC_TIMEOUT)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()
        for stream in (self._proc.stdout, self._proc.stderr):
            if stream:
                stream.close()

    def __enter__(self) -> SqlitePeer:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _peer_main(db_path: str) -> int:
    import sqlite3

    con = sqlite3.connect(db_path, isolation_level=None, timeout=0)
    try:
        for line in sys.stdin:
            command = line.split()
            try:
                if command[:1] == ['begin'] and command[1] in (
                    'IMMEDIATE',
                    'EXCLUSIVE',
                ):
                    con.execute(f'BEGIN {command[1]}')
                    reply = 'locked'
                elif command == ['begin', 'READ']:
                    # A read snapshot held open: blocks WAL resets and checkpoints, not writers.
                    con.execute('BEGIN')
                    con.execute('SELECT COUNT(*) FROM cache').fetchone()
                    reply = 'locked'
                elif command == ['rollback']:
                    con.execute('ROLLBACK')
                    reply = 'released'
                elif command == ['data_version']:
                    reply = str(con.execute('PRAGMA data_version').fetchone()[0])
                else:
                    reply = f'error: unknown command {line.strip()!r}'
            except sqlite3.Error as ex:
                reply = f'error: {type(ex).__name__}: {ex}'
            print(reply, flush=True)
    finally:
        con.close()
    return 0


def _integrity_main(db_path: str) -> int:
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        print(con.execute('PRAGMA integrity_check').fetchone()[0])
    finally:
        con.close()
    return 0


def open_cache(backend_name: str, directory: str) -> Any:
    return _backend(backend_name).make_cache(Path(directory))


# ---------------------------------------------------------------------------
# Spawned-process targets
# ---------------------------------------------------------------------------


@titled('race-add')
def add_race(backend_name: str, directory: str, key: str, barrier: Barrier, results: Any) -> None:
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        results.put((os.getpid(), cache.add(key, os.getpid())))


@titled('stats-reader')
def stats_reader(backend_name: str, directory: str, results: Any) -> None:
    with open_cache(backend_name, directory) as cache:
        results.put(cache.stats())


@titled('race-incr')
def incr_many(backend_name: str, directory: str, barrier: Barrier, ops: int) -> None:
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        for _ in range(ops):
            cache.incr('counter')


def open_fanout(backend_name: str, directory: str) -> Any:
    return _backend(backend_name).make_fanout_cache(Path(directory), shards=8)


@titled('race-fanout-add')
def fanout_add_race(backend_name: str, directory: str, keys: list[str], barrier: Barrier, results: Any) -> None:
    with open_fanout(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        results.put((os.getpid(), [key for key in keys if cache.add(key, os.getpid())]))


@titled('race-fanout-incr')
def fanout_incr_many(backend_name: str, directory: str, keys: list[str], barrier: Barrier, ops: int) -> None:
    with open_fanout(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        for i in range(ops):
            cache.incr(keys[i % len(keys)])


@titled('race-pop')
def pop_many(backend_name: str, directory: str, barrier: Barrier, keys: list[str], results: Any) -> None:
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        results.put([value for key in keys if (value := cache.pop(key)) is not None])


@titled('pop-producer')
def pop_producer(
    backend_name: str,
    directory: str,
    barrier: Barrier,
    key: str,
    tokens: int,
    produced: Event,
) -> None:
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        for n in range(tokens):
            cache.set(key, f'token-{n}')
            time.sleep(0.0005)
    produced.set()


@titled('pop-consumer')
def pop_consumer(
    backend_name: str,
    directory: str,
    barrier: Barrier,
    key: str,
    produced: Event,
    results: Any,
) -> None:
    got = []
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        while not produced.is_set():
            if (value := cache.pop(key)) is not None:
                got.append(value)
        if (value := cache.pop(key)) is not None:
            got.append(value)
    results.put(got)


@titled('overlap-holder')
def overlap_holder(
    backend_name: str,
    directory: str,
    barrier: Barrier,
    writer_done: Event,
    results: Any,
) -> None:
    """Process A: opens the cache and uses it, then stays open while B writes and exits."""
    with open_cache(backend_name, directory) as cache:
        cache.set('from-a', 'A')
        assert cache.get('from-a') == 'A'
        barrier.wait(SYNC_TIMEOUT)
        if not writer_done.wait(SYNC_TIMEOUT):
            raise TimeoutError('writer never finished')
        results.put((cache.get('from-b'), cache.get('from-b-large'), len(cache)))


@titled('overlap-writer')
def overlap_writer(backend_name: str, directory: str, barrier: Barrier) -> None:
    """Process B: opens the cache after A, writes, closes and exits."""
    barrier.wait(SYNC_TIMEOUT)
    with open_cache(backend_name, directory) as cache:
        cache.set('from-b', 'B')
        cache.set('from-b-large', b'b' * spill_min(_backend(backend_name)))


def make_blob(key: str, writer: int, n: int, rng: random.Random, min_size: int) -> bytes:
    """A value of at least `min_size` bytes that carries its own key and a SHA-256 of everything before the digest."""
    body = f'{key}|{writer}|{n}|'.encode() + rng.randbytes(rng.randrange(min_size, min_size + SPILL_SPREAD))
    return body + hashlib.sha256(body).digest()


def blob_ok(key: str, blob: Any, min_size: int) -> bool:
    if not isinstance(blob, bytes) or len(blob) < min_size:
        return False
    body, digest = blob[:-DIGEST], blob[-DIGEST:]
    return body.startswith(f'{key}|'.encode()) and hashlib.sha256(body).digest() == digest


@titled('spill-writer')
def spill_writer(
    backend_name: str,
    directory: str,
    barrier: Barrier,
    writer: int,
    keys: list[str],
    ops: int,
) -> None:
    rng = random.Random(writer)
    min_size = spill_min(_backend(backend_name))
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        for n in range(ops):
            key = rng.choice(keys)
            if rng.random() < 0.3:
                cache.delete(key)
            else:
                cache.set(key, make_blob(key, writer, n, rng, min_size))


@titled('spill-reader')
def spill_reader(
    backend_name: str,
    directory: str,
    barrier: Barrier,
    keys: list[str],
    writers_done: Event,
    results: Any,
) -> None:
    """Reads every key in turn until the writers finish, then once more. Reports (hits, misses, torn)."""
    hits = misses = 0
    torn: list[str] = []
    min_size = spill_min(_backend(backend_name))
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        last_pass = False
        while True:
            last_pass = writers_done.is_set()
            for key in keys:
                value = cache.get(key)
                if value is None:
                    misses += 1
                elif blob_ok(key, value, min_size):
                    hits += 1
                else:
                    torn.append(f'{key}: {type(value).__name__} len={len(value) if isinstance(value, bytes) else "?"}')
            if last_pass:
                break
    results.put((hits, misses, torn))


@titled('overwrite-writer')
def overwrite_writer(backend_name: str, directory: str, barrier: Barrier, key: str, ops: int) -> None:
    """Test A: overwrites one spilled key `ops` times. Never deletes it."""
    rng = random.Random(1)
    min_size = spill_min(_backend(backend_name))
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        for n in range(1, ops + 1):
            cache.set(key, make_blob(key, 0, n, rng, min_size))


@titled('overwrite-reader')
def overwrite_reader(
    backend_name: str,
    directory: str,
    barrier: Barrier,
    key: str,
    writer_done: Event,
    results: Any,
) -> None:
    """Test A: reads one always-present key with a sentinel default until the writer finishes.

    Unlike spill_reader, a None, the default or an exception is a failure: the key is never deleted.
    Reports (hits, number of failures, the first few failures).
    """
    sentinel = object()
    hits = 0
    failures: list[str] = []
    min_size = spill_min(_backend(backend_name))
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        while True:
            last_pass = writer_done.is_set()
            try:
                value = cache.get(key, sentinel)
            except Exception as ex:
                failures.append(f'raised {ex!r}')
            else:
                if value is sentinel:
                    failures.append('returned the default')
                elif value is None:
                    failures.append('returned None')
                elif blob_ok(key, value, min_size):
                    hits += 1
                else:
                    failures.append(
                        f'torn: {type(value).__name__} len={len(value) if isinstance(value, bytes) else "?"}'
                    )
            if last_pass:
                break
    results.put((hits, len(failures), failures[:5]))


@titled('race-open')
def open_new_directory(backend_name: str, directory: str, barrier: Barrier, worker: int, results: Any) -> None:
    """Test B: every worker opens the same not-yet-existing directory at once, then sets and gets.

    Reports (worker, None) on success or (worker, traceback) on any failure, so a crash in one
    worker is visible instead of just a non-zero exit code.
    """
    try:
        barrier.wait(SYNC_TIMEOUT)
        with open_cache(backend_name, directory) as cache:
            cache.set(f'worker-{worker}', worker)
            got = cache.get(f'worker-{worker}')
            results.put((worker, None if got == worker else f'read back {got!r}'))
    except BaseException:
        results.put((worker, traceback.format_exc()))


@titled('kill-target')
def killable_spill_writer(backend_name: str, directory: str, keys: list[str], seed: int, started: Event) -> None:
    """Test I: overwrites spilled values forever, until the parent SIGKILLs it."""
    rng = random.Random(seed)
    min_size = spill_min(_backend(backend_name))
    with open_cache(backend_name, directory) as cache:
        n = 0
        while True:
            key = rng.choice(keys)
            cache.set(key, make_blob(key, seed, n, rng, min_size))
            n += 1
            if n == 3:
                started.set()


# ---------------------------------------------------------------------------
# Fork checks (run in a fresh interpreter, see the module docstring)
# ---------------------------------------------------------------------------

FORK_CHILDREN = 2
FORK_OPS = 200
# How long a forked child gets to finish before the parent kills it and reports a hang.
CHILD_DEADLINE = 15.0


def _fork_child_body(cache: Any, barrier: Barrier, child: int, size: int) -> None:
    # The child keeps using the Cache object it inherited from the parent.
    assert cache.get('before-fork') == 'parent', 'child cannot read what the parent wrote before forking'
    barrier.wait(SYNC_TIMEOUT)
    for _ in range(FORK_OPS):
        cache.incr('counter')
    cache.set(f'child-{child}', b'c' * size)


def _fork_child_entry(cache: Any, barrier: Barrier, child: int, size: int) -> None:
    """Target for the fork context. Exits non-zero (via the exception) on failure."""
    _fork_child_body(cache, barrier, child, size)


def _os_fork(body: Any) -> int:
    """os.fork() and run `body()` in the child, which exits 0 on success and 1 on any exception."""
    pid = os.fork()
    if pid == 0:
        # No set_title() here: setproctitle hangs in a child forked from a multi-threaded process.
        # The child keeps its parent's `dco-fork-check` title.
        code = 0
        try:
            body()
        except BaseException:
            traceback.print_exc()
            code = 1
        finally:
            os._exit(code)
    return pid


def _wait_or_kill(pid: int, deadline: float = CHILD_DEADLINE) -> int | None:
    """The child's exit code, or None if it was still running at the deadline (it is then killed)."""
    end = time.monotonic() + deadline
    while time.monotonic() < end:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        time.sleep(0.01)
    os.kill(pid, 9)
    os.waitpid(pid, 0)
    return None


def _fork_children(cache: Any, backend: Backend, mode: str) -> tuple[list[int | None], list[str]]:
    """The original check: two children and the parent incr together; children write a spilled value."""
    size = spill_min(backend)
    ctx = multiprocessing.get_context('fork')
    barrier = ctx.Barrier(FORK_CHILDREN + 1)
    if mode == 'os-fork':
        pids = [
            _os_fork(lambda child=child: _fork_child_body(cache, barrier, child, size))
            for child in range(FORK_CHILDREN)
        ]
        barrier.wait(SYNC_TIMEOUT)
        for _ in range(FORK_OPS):
            cache.incr('counter')
        exit_codes = [_wait_or_kill(pid, SYNC_TIMEOUT) for pid in pids]
    else:
        procs = [
            ctx.Process(target=_fork_child_entry, args=(cache, barrier, child, size)) for child in range(FORK_CHILDREN)
        ]
        for p in procs:
            p.start()
        barrier.wait(SYNC_TIMEOUT)
        for _ in range(FORK_OPS):
            cache.incr('counter')
        for p in procs:
            p.join(SYNC_TIMEOUT)
        exit_codes = [p.exitcode for p in procs]

    failures = []
    # The parent's cache object still works after forking, and sees the children's writes.
    expected = (FORK_CHILDREN + 1) * FORK_OPS
    if cache.get('counter') != expected:
        failures.append(f'counter {cache.get("counter")} != {expected}')
    for child in range(FORK_CHILDREN):
        if cache.get(f'child-{child}') != b'c' * size:
            failures.append(f'parent cannot read child-{child}')
    return exit_codes, failures


def _forked_get_and_set(cache: Any) -> None:
    """The child's whole job in the thread modes: one read and one write on the inherited Cache."""
    assert cache.get('before-fork') == 'parent'
    assert cache.set('from-child', 'child') is True
    assert cache.get('from-child') == 'child'


def _fork_while_hammered(cache: Any, writes: bool) -> tuple[list[int | None], list[str]]:
    """Fork while another parent thread is mid-get (and, with `writes`, mid-set) on the same Cache.

    A child that inherits a mutex, or SQLite lock state, that a parent thread held at the moment of
    the fork has no thread left to release it, and its first operation stalls.
    """
    stop = threading.Event()
    errors: list[BaseException] = []
    ops = [0]

    def hammer() -> None:
        try:
            while not stop.is_set():
                if writes:
                    cache.set('busy', ops[0])
                cache.get('busy')
                cache.get('before-fork')
                ops[0] += 1
        except BaseException as ex:
            errors.append(ex)

    thread = threading.Thread(target=hammer, name='hammer')
    thread.start()
    exit_codes: list[int | None] = []
    try:
        for _ in range(FORK_CHILDREN):
            # Fork only once the hammer is demonstrably mid-run.
            start = ops[0]
            while ops[0] < start + 20 and thread.is_alive():
                time.sleep(0.001)
            exit_codes.append(_wait_or_kill(_os_fork(lambda: _forked_get_and_set(cache))))
    finally:
        stop.set()
        thread.join(SYNC_TIMEOUT)

    failures = [f'hammer thread failed: {ex!r}' for ex in errors]
    if thread.is_alive():
        failures.append('hammer thread hung')
    if cache.get('from-child') != 'child':
        failures.append('parent cannot read the child write')
    return exit_codes, failures


def _fork_in_write_transaction(cache: Any, backend: Backend) -> tuple[list[int | None], list[str]]:
    """Fork while another parent thread holds the cache's write transaction (backend.hold_write_transaction).

    The deterministic worst case of _fork_while_hammered(writes=True). The holder commits on its own
    after HOLD_SECONDS rather than waiting for the fork: a backend with a fork barrier waits for that
    commit before forking, and it would deadlock against a holder that waited for the fork. A
    backend without one forks mid-transaction, and the child is left behind a lock nobody releases.

    Prints the fork and commit times (relative to the holder entering its transaction) to stderr:
    if the fork returned only after the commit on a backend with no barrier (a loaded machine
    that stalled the main thread for HOLD_SECONDS), the child never saw the lock and the strict
    xfail turns into a confusing XPASS.
    """
    inside = threading.Event()
    errors: list[BaseException] = []
    times: dict[str, float] = {}

    def holder() -> None:
        try:
            with backend.hold_write_transaction(cache):
                cache.set('busy', 1)
                times['entered'] = time.monotonic()
                inside.set()
                time.sleep(HOLD_SECONDS)
            times['committed'] = time.monotonic()
        except BaseException as ex:
            errors.append(ex)
            inside.set()

    thread = threading.Thread(target=holder, name='holder')
    thread.start()
    try:
        assert inside.wait(SYNC_TIMEOUT), 'holder never entered its transaction'
        times['fork-called'] = time.monotonic()
        pid = _os_fork(lambda: _forked_get_and_set(cache))
        times['fork-returned'] = time.monotonic()
    finally:
        thread.join(SYNC_TIMEOUT)
    exit_codes = [_wait_or_kill(pid)]

    if 'entered' in times:
        rel = {k: f'{v - times["entered"]:.3f}s' for k, v in times.items() if k != 'entered'}
        print(
            f'timing (since the holder entered its transaction, HOLD_SECONDS={HOLD_SECONDS}): {rel}',
            file=sys.stderr,
        )
    failures = [f'holder thread failed: {ex!r}' for ex in errors]
    if cache.get('busy') != 1:
        failures.append('the holder transaction did not commit')
    if cache.get('from-child') != 'child':
        failures.append('parent cannot read the child write')
    return exit_codes, failures


# How long the holder in _fork_in_write_transaction keeps its transaction open.
HOLD_SECONDS = 1.0


def _fork_child_closes(cache: Any, backend: Backend) -> tuple[list[int | None], list[str]]:
    """A forked child closes its inherited Cache and exits; the parent must keep working."""
    size = spill_min(backend)
    exit_codes = []
    for _ in range(FORK_CHILDREN):
        exit_codes.append(_wait_or_kill(_os_fork(cache.close)))

    failures = []
    for i in range(FORK_OPS):
        cache.set(f'after-{i}', b'a' * (size if i % 20 == 0 else 10))
        cache.incr('counter')
    if cache.get('before-fork') != 'parent':
        failures.append('parent lost a value written before the fork')
    if not all(cache.get(f'after-{i}') is not None for i in range(FORK_OPS)):
        failures.append('parent cannot read its own writes after the child closed')
    if cache.get('counter') != FORK_OPS:
        failures.append(f'counter {cache.get("counter")} != {FORK_OPS}')
    return exit_codes, failures


def _fork_two_caches_idle(a: Any, backend: Backend, directory: Path) -> tuple[list[int | None], list[str]]:
    """Test C, variant 1: two Cache objects on one directory, both used, then an idle fork.

    The child reads and writes through both inherited objects while the parent keeps incrementing
    through both.
    """
    size = spill_min(backend)
    b = backend.make_cache(directory)
    a.set('from-a', 'A')
    b.set('from-b', b'b' * size)
    assert b.get('from-a') == 'A' and a.get('from-b') == b'b' * size
    barrier = multiprocessing.get_context('fork').Barrier(2)

    def child() -> None:
        assert a.get('from-b') == b'b' * size and b.get('from-a') == 'A'
        barrier.wait(SYNC_TIMEOUT)
        for i in range(FORK_OPS):
            (a if i % 2 else b).incr('counter')
        assert a.set('child-a', 'a') is True
        assert b.set('child-b', b'c' * size) is True
        assert b.get('child-a') == 'a' and a.get('child-b') == b'c' * size

    pid = _os_fork(child)
    barrier.wait(SYNC_TIMEOUT)
    for i in range(FORK_OPS):
        (b if i % 2 else a).incr('counter')
    exit_codes = [_wait_or_kill(pid)]

    failures = []
    for name, handle in (('a', a), ('b', b)):
        if handle.get('counter') != 2 * FORK_OPS:
            failures.append(f'{name}: counter {handle.get("counter")} != {2 * FORK_OPS}')
        if handle.get('child-a') != 'a' or handle.get('child-b') != b'c' * size:
            failures.append(f'{name}: parent cannot read the child writes')
    b.close()
    return exit_codes, failures


def _fork_while_other_cache_writes(a: Any, backend: Backend, directory: Path) -> tuple[list[int | None], list[str]]:
    """Test C, variant 2: a parent thread hammers writes through `b` while the main thread forks with `a`.

    The child's inherited `a` was idle, so only a fork barrier that covers every Cache in the
    process (not just the one being used) keeps the child out of b's half-finished transaction.
    """
    b = backend.make_cache(directory, timeout=THREAD_MODE_TIMEOUT)
    b.set('busy', 0)
    stop = threading.Event()
    errors: list[BaseException] = []
    ops = [0]

    def hammer() -> None:
        try:
            while not stop.is_set():
                b.set('busy', ops[0])
                b.get('busy')
                ops[0] += 1
        except BaseException as ex:
            errors.append(ex)

    def child() -> None:
        _forked_get_and_set(a)
        assert b.set('from-child-b', 'child') is True
        assert a.get('from-child-b') == 'child'

    thread = threading.Thread(target=hammer, name='hammer')
    thread.start()
    exit_codes: list[int | None] = []
    try:
        for _ in range(FORK_CHILDREN):
            start = ops[0]
            while ops[0] < start + 20 and thread.is_alive():
                time.sleep(0.001)
            exit_codes.append(_wait_or_kill(_os_fork(child)))
    finally:
        stop.set()
        thread.join(SYNC_TIMEOUT)

    failures = [f'hammer thread failed: {ex!r}' for ex in errors]
    if thread.is_alive():
        failures.append('hammer thread hung')
    if a.get('from-child') != 'child' or b.get('from-child-b') != 'child':
        failures.append('parent cannot read the child writes')
    b.close()
    return exit_codes, failures


def _fork_then_open_new_cache(cache: Any, backend: Backend, directory: Path) -> tuple[list[int | None], list[str]]:
    """Test G: after the parent used a cache, a forked child opens a brand-new Cache elsewhere and uses it."""
    other = directory.parent / f'{directory.name}-forked-child'
    size = spill_min(backend)

    def child() -> None:
        with backend.make_cache(other) as fresh:
            assert fresh.set('small', 'child') is True
            assert fresh.set('large', b'g' * size) is True
            assert fresh.get('small') == 'child' and fresh.get('large') == b'g' * size
        # The inherited cache still works too.
        assert cache.get('before-fork') == 'parent'

    exit_codes = [_wait_or_kill(_os_fork(child))]
    failures = []
    with backend.make_cache(other) as fresh:
        if fresh.get('small') != 'child' or fresh.get('large') != b'g' * size:
            failures.append("parent cannot read the child's new cache")
    result = integrity(backend.db_path(other))
    if result != 'ok':
        failures.append(f"integrity_check of the child's cache: {result}")
    return exit_codes, failures


# Called as mode(cache, backend).
FORK_MODES = {
    'os-fork': lambda cache, backend: _fork_children(cache, backend, 'os-fork'),
    'fork-context': lambda cache, backend: _fork_children(cache, backend, 'fork-context'),
    'fork-while-reading': lambda cache, backend: _fork_while_hammered(cache, writes=False),
    'fork-while-writing': lambda cache, backend: _fork_while_hammered(cache, writes=True),
    'fork-in-write-txn': _fork_in_write_transaction,
    'child-closes': _fork_child_closes,
}

# Modes that open further caches: called as mode(cache, backend, directory).
MULTI_CACHE_MODES = {
    'two-caches-idle': _fork_two_caches_idle,
    'two-caches-other-writing': _fork_while_other_cache_writes,
    'fork-then-new-cache': _fork_then_open_new_cache,
}

ALL_FORK_MODES = (*FORK_MODES, *MULTI_CACHE_MODES)

# The thread modes open the cache with a short timeout, so a child stuck behind a lock nobody will
# release fails with the backend's Timeout in about a second instead of running into the deadline.
THREAD_MODE_TIMEOUT = 1.0
THREAD_MODES = frozenset(
    {
        'fork-while-reading',
        'fork-while-writing',
        'fork-in-write-txn',
        'two-caches-other-writing',
    }
)


def fork_check(backend_name: str, directory: str, mode: str) -> int:
    backend = _backend(backend_name)
    options: dict[str, Any] = {'timeout': THREAD_MODE_TIMEOUT} if mode in THREAD_MODES else {}
    cache = backend.make_cache(Path(directory), **options)
    # Use the cache before forking so the parent holds an open connection.
    cache.set('before-fork', 'parent')
    cache.incr('counter', 0)
    assert cache.get('before-fork') == 'parent'

    if mode in MULTI_CACHE_MODES:
        exit_codes, failures = MULTI_CACHE_MODES[mode](cache, backend, Path(directory))
    else:
        exit_codes, failures = FORK_MODES[mode](cache, backend)
    if exit_codes != [0] * len(exit_codes) or not exit_codes:
        failures.insert(0, f'child exit codes {exit_codes} (None means it hung and was killed)')
    cache.close()
    result = integrity(backend.db_path(Path(directory)))
    if result != 'ok':
        failures.append(f'integrity_check: {result}')
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# Phase 0.5 Tests D and E: thread-close scenario and WAL checkpoint (CLI commands)
#
#   python -W error::ResourceWarning -m tests.conformance.proc_workers threads-close <backend> <directory> <threads>
#       <threads> threads each do one get and one set on a shared Cache, exit, then the main
#       thread closes it and collects garbage. sqlite3 reports unclosed connections from dealloc
#       as unraisables, so the caller looks for "ResourceWarning" in stderr. Exits non-zero if a
#       worker failed or hung, or the cache doesn't hold every write, so a run where the threads
#       never really used the cache can't pass for a clean close.
#
#   python -m tests.conformance.proc_workers checkpoint <db>
#       PRAGMA wal_checkpoint(TRUNCATE); prints the (busy, log, checkpointed) row.
# ---------------------------------------------------------------------------


def threads_close_main(backend_name: str, directory: str, threads: int) -> int:
    import gc

    cache = open_cache(backend_name, directory)
    cache.set('main', 0)
    errors: list[str] = []

    def work(tid: int) -> None:
        try:
            assert cache.get('main') == 0
            assert cache.set(f'thread-{tid}', tid) is True
        except BaseException:
            errors.append(f'thread {tid}: {traceback.format_exc()}')

    workers = [threading.Thread(target=work, args=(tid,)) for tid in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(SYNC_TIMEOUT)
    failures = list(errors)
    if hung := sum(t.is_alive() for t in workers):
        failures.append(f'{hung} worker thread(s) hung')
    # 'main' plus one key per thread.
    if (count := len(cache)) != threads + 1:
        failures.append(f'len(cache) == {count}, expected {threads + 1}')
    cache.close()
    gc.collect()
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


def _checkpoint_main(db_path: str) -> int:
    import sqlite3

    con = sqlite3.connect(db_path, isolation_level=None)
    try:
        print(con.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone())
    finally:
        con.close()
    return 0


def checkpoint(db_path: Path) -> None:
    """Fold the WAL into the main database file, in a helper subprocess (see the module docstring)."""
    proc = subprocess.run(
        [
            sys.executable,
            '-m',
            'tests.conformance.proc_workers',
            'checkpoint',
            str(db_path),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=SYNC_TIMEOUT,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'checkpoint helper failed: {proc.stderr}')


USAGE = f"""usage:
  {sys.argv[0]} fork <backend> <directory> <{'|'.join(ALL_FORK_MODES)}>
  {sys.argv[0]} peer <db>
  {sys.argv[0]} integrity <db>"""


# Process titles for the CLI commands. `peer`, `integrity` and `checkpoint` are stdlib sqlite3
# helpers; the others exercise the cache under test.
CLI_TITLES = {
    'fork': 'fork-check',
    'threads-close': 'threads-close',
    'peer': 'sqlite-peer',
    'integrity': 'sqlite-integrity',
    'checkpoint': 'sqlite-checkpoint',
}

if __name__ == '__main__':
    args = sys.argv[1:]
    set_title(CLI_TITLES.get(args[0] if args else '', 'proc-workers'))
    if args[:1] == ['fork'] and len(args) == 4 and args[3] in ALL_FORK_MODES:
        sys.exit(fork_check(args[1], args[2], args[3]))
    if args[:1] == ['peer'] and len(args) == 2:
        sys.exit(_peer_main(args[1]))
    if args[:1] == ['integrity'] and len(args) == 2:
        sys.exit(_integrity_main(args[1]))
    if args[:1] == ['threads-close'] and len(args) == 4:
        sys.exit(threads_close_main(args[1], args[2], int(args[3])))
    if args[:1] == ['checkpoint'] and len(args) == 2:
        sys.exit(_checkpoint_main(args[1]))
    sys.exit(USAGE)
