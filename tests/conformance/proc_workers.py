"""Module-level process targets for test_processes.py, plus a small CLI.

Spawned children import this module by name, so it must stay importable from the repo root and
must not import pytest fixtures. The CLI runs in a fresh interpreter (cwd = repo root):

    python -m tests.conformance.proc_workers fork <backend> <directory> <mode>
        The fork checks (see FORK_MODES), isolated so macOS warnings about forking a process
        that has threads can't touch the pytest session. Exit code 0 on success; failures print
        to stderr.

    python -m tests.conformance.proc_workers peer <db>
        A second SQLite connection on <db>, driven one command per stdin line, one reply line each:
        `begin <IMMEDIATE|EXCLUSIVE>` -> `locked`, `rollback` -> `released`,
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

if TYPE_CHECKING:
    from multiprocessing.synchronize import Barrier, Event

    from tests.conformance.backends import Backend

REPO_ROOT = Path(__file__).resolve().parents[2]
SYNC_TIMEOUT = 30.0
SPILL_MIN = 100_000
SPILL_MAX = 300_000
DIGEST = 32


def _backend(name: str) -> Backend:
    # Imported lazily so the peer and integrity commands start fast and never load a cache backend.
    from tests.conformance.backends import BACKENDS

    return BACKENDS[name]()


def spill_min(backend: Backend) -> int:
    """The smallest blob size: at least 100 KB, and always at least twice the spill threshold."""
    return max(SPILL_MIN, 2 * backend.spill_threshold)


def integrity(db_path: Path) -> str:
    """PRAGMA integrity_check, run in a helper subprocess (see the module docstring)."""
    proc = subprocess.run(
        [sys.executable, '-m', 'tests.conformance.proc_workers', 'integrity', str(db_path)],
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
            [sys.executable, '-m', 'tests.conformance.proc_workers', 'peer', str(db_path)],
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
            raise RuntimeError(f'peer {command!r} failed: {reply} {self._proc.stderr.read() if self._proc.stderr else ""}')
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
                if command[:1] == ['begin'] and command[1] in ('IMMEDIATE', 'EXCLUSIVE'):
                    con.execute(f'BEGIN {command[1]}')
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


def add_race(backend_name: str, directory: str, key: str, barrier: Barrier, results: Any) -> None:
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        results.put((os.getpid(), cache.add(key, os.getpid())))


def incr_many(backend_name: str, directory: str, barrier: Barrier, ops: int) -> None:
    with open_cache(backend_name, directory) as cache:
        barrier.wait(SYNC_TIMEOUT)
        for _ in range(ops):
            cache.incr('counter')


def overlap_holder(backend_name: str, directory: str, barrier: Barrier, writer_done: Event, results: Any) -> None:
    """Process A: opens the cache and uses it, then stays open while B writes and exits."""
    with open_cache(backend_name, directory) as cache:
        cache.set('from-a', 'A')
        assert cache.get('from-a') == 'A'
        barrier.wait(SYNC_TIMEOUT)
        if not writer_done.wait(SYNC_TIMEOUT):
            raise TimeoutError('writer never finished')
        results.put((cache.get('from-b'), cache.get('from-b-large'), len(cache)))


def overlap_writer(backend_name: str, directory: str, barrier: Barrier) -> None:
    """Process B: opens the cache after A, writes, closes and exits."""
    barrier.wait(SYNC_TIMEOUT)
    with open_cache(backend_name, directory) as cache:
        cache.set('from-b', 'B')
        cache.set('from-b-large', b'b' * SPILL_MIN)


def make_blob(key: str, writer: int, n: int, rng: random.Random, min_size: int) -> bytes:
    """A value of at least `min_size` bytes that carries its own key and a SHA-256 of everything before the digest."""
    body = f'{key}|{writer}|{n}|'.encode() + rng.randbytes(rng.randrange(min_size, min_size + SPILL_MAX))
    return body + hashlib.sha256(body).digest()


def blob_ok(key: str, blob: Any, min_size: int) -> bool:
    if not isinstance(blob, bytes) or len(blob) < min_size:
        return False
    body, digest = blob[:-DIGEST], blob[-DIGEST:]
    return body.startswith(f'{key}|'.encode()) and hashlib.sha256(body).digest() == digest


def spill_writer(backend_name: str, directory: str, barrier: Barrier, writer: int, keys: list[str], ops: int) -> None:
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


def spill_reader(backend_name: str, directory: str, barrier: Barrier, keys: list[str], writers_done: Event, results: Any) -> None:
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


# ---------------------------------------------------------------------------
# Fork checks (run in a fresh interpreter, see the module docstring)
# ---------------------------------------------------------------------------

FORK_CHILDREN = 2
FORK_OPS = 200
# How long a forked child gets to finish before the parent kills it and reports a hang.
CHILD_DEADLINE = 15.0


def _fork_child_body(cache: Any, barrier: Barrier, child: int) -> None:
    # The child keeps using the Cache object it inherited from the parent.
    assert cache.get('before-fork') == 'parent', 'child cannot read what the parent wrote before forking'
    barrier.wait(SYNC_TIMEOUT)
    for _ in range(FORK_OPS):
        cache.incr('counter')
    cache.set(f'child-{child}', b'c' * SPILL_MIN)


def _fork_child_entry(cache: Any, barrier: Barrier, child: int) -> None:
    """Target for the fork context. Exits non-zero (via the exception) on failure."""
    _fork_child_body(cache, barrier, child)


def _os_fork(body: Any) -> int:
    """os.fork() and run `body()` in the child, which exits 0 on success and 1 on any exception."""
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


def _fork_children(cache: Any, mode: str) -> tuple[list[int | None], list[str]]:
    """The original check: two children and the parent incr together; children write a spilled value."""
    ctx = multiprocessing.get_context('fork')
    barrier = ctx.Barrier(FORK_CHILDREN + 1)
    if mode == 'os-fork':
        pids = [_os_fork(lambda child=child: _fork_child_body(cache, barrier, child)) for child in range(FORK_CHILDREN)]
        barrier.wait(SYNC_TIMEOUT)
        for _ in range(FORK_OPS):
            cache.incr('counter')
        exit_codes = [_wait_or_kill(pid, SYNC_TIMEOUT) for pid in pids]
    else:
        procs = [ctx.Process(target=_fork_child_entry, args=(cache, barrier, child)) for child in range(FORK_CHILDREN)]
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
        if cache.get(f'child-{child}') != b'c' * SPILL_MIN:
            failures.append(f'parent cannot read child-{child}')
    return exit_codes, failures


def _fork_while_busy(cache: Any) -> tuple[list[int | None], list[str]]:
    """Fork while another parent thread is mid-get/set on the same Cache.

    If the Cache (or SQLite under it) holds a mutex at the moment of the fork, the child inherits
    it locked, with no thread left to release it, and its first operation deadlocks.
    """
    stop = threading.Event()
    errors: list[BaseException] = []
    ops = [0]

    def hammer() -> None:
        try:
            while not stop.is_set():
                cache.set('busy', ops[0])
                cache.get('busy')
                cache.get('before-fork')
                ops[0] += 1
        except BaseException as ex:
            errors.append(ex)

    def child_body() -> None:
        assert cache.get('before-fork') == 'parent'
        assert cache.set('from-child', 'child') is True
        assert cache.get('from-child') == 'child'

    thread = threading.Thread(target=hammer, name='hammer')
    thread.start()
    exit_codes: list[int | None] = []
    try:
        for _ in range(FORK_CHILDREN):
            # Fork only once the hammer is demonstrably mid-run.
            start = ops[0]
            while ops[0] < start + 20 and thread.is_alive():
                time.sleep(0.001)
            exit_codes.append(_wait_or_kill(_os_fork(child_body)))
    finally:
        stop.set()
        thread.join(SYNC_TIMEOUT)

    failures = [f'hammer thread failed: {ex!r}' for ex in errors]
    if thread.is_alive():
        failures.append('hammer thread hung')
    if cache.get('from-child') != 'child':
        failures.append('parent cannot read the child write')
    return exit_codes, failures


def _fork_child_closes(cache: Any) -> tuple[list[int | None], list[str]]:
    """A forked child closes its inherited Cache and exits; the parent must keep working."""
    exit_codes = []
    for _ in range(FORK_CHILDREN):
        exit_codes.append(_wait_or_kill(_os_fork(cache.close)))

    failures = []
    for i in range(FORK_OPS):
        cache.set(f'after-{i}', b'a' * (SPILL_MIN if i % 20 == 0 else 10))
        cache.incr('counter')
    if cache.get('before-fork') != 'parent':
        failures.append('parent lost a value written before the fork')
    if not all(cache.get(f'after-{i}') is not None for i in range(FORK_OPS)):
        failures.append('parent cannot read its own writes after the child closed')
    if cache.get('counter') != FORK_OPS:
        failures.append(f'counter {cache.get("counter")} != {FORK_OPS}')
    return exit_codes, failures


FORK_MODES = {
    'os-fork': lambda cache: _fork_children(cache, 'os-fork'),
    'fork-context': lambda cache: _fork_children(cache, 'fork-context'),
    'fork-while-busy': _fork_while_busy,
    'child-closes': _fork_child_closes,
}


def fork_check(backend_name: str, directory: str, mode: str) -> int:
    backend = _backend(backend_name)
    cache = backend.make_cache(Path(directory))
    # Use the cache before forking so the parent holds an open connection.
    cache.set('before-fork', 'parent')
    cache.incr('counter', 0)
    assert cache.get('before-fork') == 'parent'

    exit_codes, failures = FORK_MODES[mode](cache)
    if exit_codes != [0] * len(exit_codes) or not exit_codes:
        failures.insert(0, f'child exit codes {exit_codes} (None means it hung and was killed)')
    cache.close()
    result = integrity(backend.db_path(Path(directory)))
    if result != 'ok':
        failures.append(f'integrity_check: {result}')
    for failure in failures:
        print(failure, file=sys.stderr)
    return 1 if failures else 0


USAGE = f"""usage:
  {sys.argv[0]} fork <backend> <directory> <{'|'.join(FORK_MODES)}>
  {sys.argv[0]} peer <db>
  {sys.argv[0]} integrity <db>"""


if __name__ == '__main__':
    args = sys.argv[1:]
    if args[:1] == ['fork'] and len(args) == 4 and args[3] in FORK_MODES:
        sys.exit(fork_check(args[1], args[2], args[3]))
    if args[:1] == ['peer'] and len(args) == 2:
        sys.exit(_peer_main(args[1]))
    if args[:1] == ['integrity'] and len(args) == 2:
        sys.exit(_integrity_main(args[1]))
    sys.exit(USAGE)
