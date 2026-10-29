"""A soak worker: one process hammering the chaos and ledger caches from several threads.

Started by soak/soak.py, never by hand:
    python -m soak.worker run <run_dir> <worker_id> <threads> <seed> [<every> <ops/s per thread>]
    python -m soak.worker maint <run_dir> clear|expire|cull

Two caches share the run directory:
- chaos/: keys shared by every worker, expiring entries, a small size_limit (so culling runs), large
  values that spill to files, and clear()/expire()/cull() from maintenance processes. Anything may
  vanish here; what's checked is that whatever comes back is intact and not expired.
- ledger/: nothing expires, is cleared or is culled, so every acknowledged write must survive:
  - `ctr:<worker>` counters, against the count of incr() calls that returned;
  - `own:<worker>:<n>` keys with a single writer, which must read back exactly what was last written;
  - `once:<slot>` add() races, which must have exactly one winner.

Every value carries its key and a CRC of its payload, so corruption or a value landing under the
wrong key is caught on every read.

The worker writes its counters to stats/<worker>.json every few seconds (atomically), and its
problems to the same file, for the orchestrator to collect. SIGUSR1 asks for a fork test (fork
while the threads run; the child must read and write both caches and exit within a deadline).
SIGUSR2 dumps every thread's traceback to stderr (for stall diagnosis). SIGTERM stops it cleanly. It then verifies every owned key, and its final stats carry the exact
acknowledged counter value for the end-of-run check.
"""

from __future__ import annotations

import faulthandler
import json
import os
import random
import signal
import sys
import threading
import time
import traceback
import warnings
import zlib
from pathlib import Path
from typing import Any

import diskcache_oxide
from diskcache_oxide import Cache, Timeout

STATS_EVERY = 5.0
CHAOS_KEYS = 20_000
CHAOS_SIZE_LIMIT = 256 * 2**20
OWNED_KEYS = 200
ONCE_SLOTS_PER_MINUTE = 20
LATENCY_SAMPLE_EVERY = 50
MAX_PROBLEMS_KEPT = 50
FORK_DEADLINE = 20.0
# A get that started this long after a value's expire time must not return it (the writer's and
# reader's clock reads aren't simultaneous).
EXPIRY_SLACK = 1.0


def open_chaos(run_dir: Path) -> Cache:
    return Cache(run_dir / 'chaos', timeout=30.0, size_limit=CHAOS_SIZE_LIMIT)


def open_ledger(run_dir: Path) -> Cache:
    return Cache(run_dir / 'ledger', timeout=60.0, size_limit=2**36)


# ---------------------------------------------------------------------------
# Self-checking values
# ---------------------------------------------------------------------------


def make_value(key: str, rng: random.Random, expire_at: float | None) -> Any:
    roll = rng.random()
    if roll < 0.90:
        body = 'x' * rng.randrange(10, 400)
        return {'key': key, 'crc': zlib.crc32(body.encode()), 'body': body, 'exp': expire_at, 'n': rng.randrange(1 << 40)}
    size = rng.randrange(1_024, 8_192) if roll < 0.99 else rng.randrange(64 * 1024, 512 * 1024)
    payload = rng.randbytes(size)
    header = json.dumps({'key': key, 'crc': zlib.crc32(payload), 'exp': expire_at}).encode()
    return len(header).to_bytes(4, 'big') + header + payload


def check_value(key: str, value: Any, now: float) -> str | None:
    """None if `value` is intact and current, else what's wrong with it."""
    if isinstance(value, dict):
        if value.get('key') != key:
            return f'value under {key!r} belongs to {value.get("key")!r}'
        if zlib.crc32(str(value.get('body', '')).encode()) != value.get('crc'):
            return f'dict value under {key!r} fails its CRC'
        expire_at = value.get('exp')
    elif isinstance(value, bytes):
        n = int.from_bytes(value[:4], 'big')
        try:
            header = json.loads(value[4 : 4 + n])
        except ValueError:
            return f'bytes value under {key!r} has a corrupt header'
        if header.get('key') != key:
            return f'bytes value under {key!r} belongs to {header.get("key")!r}'
        if zlib.crc32(value[4 + n :]) != header.get('crc'):
            return f'bytes value under {key!r} ({len(value)} bytes) fails its CRC'
        expire_at = header.get('exp')
    else:
        return f'unexpected value type under {key!r}: {type(value).__name__}'
    if expire_at is not None and now > expire_at + EXPIRY_SLACK:
        return f'{key!r} returned {now - expire_at:.1f}s after it expired'
    return None


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class Worker:
    def __init__(self, run_dir: Path, worker_id: int, threads: int, seed: int, every: int = 0, thread_rate: float = 0.0) -> None:
        # every: one write per this many ops (0 alternates the 99:1 and 1,000:1 mixes by thread).
        # thread_rate: ops per second per thread (0 runs flat out).
        self.every = every
        self.thread_rate = thread_rate
        self.run_dir = run_dir
        self.id = worker_id
        self.thread_count = threads
        self.seed = seed
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.chaos = open_chaos(run_dir)
        self.ledger = open_ledger(run_dir)
        # Protected by self.lock.
        self.ops = 0
        self.misses = 0
        self.timeouts = 0
        self.reopens = 0
        self.acked = 0  # incr('ctr:<id>') calls that returned
        self.owned: dict[str, int] = {}  # own:<id>:<n> -> the version last acknowledged
        self.once_won: list[str] = []
        self.once_tried = 0
        self.forks_ok = 0
        self.forks_failed = 0
        self.problems: list[str] = []
        self.problem_count = 0
        self.latency: dict[str, list[int]] = {'get': [], 'set': []}
        self.fork_requested = threading.Event()

    def problem(self, text: str) -> None:
        with self.lock:
            self.problem_count += 1
            if len(self.problems) < MAX_PROBLEMS_KEPT:
                self.problems.append(f'{time.strftime("%H:%M:%S")} {text}')

    # -- the load -----------------------------------------------------------------------------

    def thread_main(self, n: int) -> None:
        rng = random.Random(self.seed * 1_000 + n)
        # The 99:1 headline mix and the 1,000:1 Talk Python profile, alternating by thread, unless
        # --mix fixed one.
        every = self.every or (100 if n % 2 == 0 else 1_000)
        interval = 1.0 / self.thread_rate if self.thread_rate else 0.0
        next_at = time.monotonic() + rng.uniform(0, interval)
        i = 0
        while not self.stop.is_set():
            if interval:  # Paced: a steady request rate rather than flat out.
                delay = next_at - time.monotonic()
                if delay > 0 and self.stop.wait(delay):
                    break
                next_at = max(next_at + interval, time.monotonic() - interval)
            i += 1
            try:
                self.one_op(rng, i, every)
            except Timeout:
                with self.lock:
                    self.timeouts += 1
            except Exception:
                self.problem(f'thread {n}: {traceback.format_exc(limit=6)}')
                time.sleep(0.1)

    def one_op(self, rng: random.Random, i: int, every: int) -> None:
        key = f'c:{rng.randrange(CHAOS_KEYS)}'
        # Paced runs are slow enough to time every call, so the maximum is the true worst case.
        sample = self.thread_rate > 0 or i % LATENCY_SAMPLE_EVERY == 0
        if i % every == 0:
            expire = rng.choice((None, None, rng.uniform(1, 60)))
            expire_at = None if expire is None else time.time() + expire
            value = make_value(key, rng, expire_at)
            start = time.perf_counter_ns()
            self.chaos.set(key, value, expire=expire)
            self.record('set', time.perf_counter_ns() - start)  # Writes are rare enough to time every one.
            self.ledger_op(rng)
        else:
            asked_at = time.time()  # Expired means expired before we asked, not by the time we look.
            start = time.perf_counter_ns()
            value = self.chaos.get(key)
            if sample:
                self.record('get', time.perf_counter_ns() - start)
            if value is None:
                with self.lock:
                    self.misses += 1
            elif (wrong := check_value(key, value, asked_at)) is not None:
                self.problem(wrong)
        if i % 5_000 == 0 and rng.random() < 0.05:
            self.reopen()
        with self.lock:
            self.ops += 1

    def ledger_op(self, rng: random.Random) -> None:
        roll = rng.random()
        if roll < 0.5:
            self.ledger.incr(f'ctr:{self.id}')
            with self.lock:
                self.acked += 1
        elif roll < 0.9:
            key = f'own:{self.id}:{rng.randrange(OWNED_KEYS)}'
            with self.lock:
                version = self.owned.get(key, 0) + 1
            # Only this worker writes own:<id>:*, but two of its threads may pick the same key;
            # the lock keeps "last acknowledged version" exact.
            with self.lock:
                assert self.ledger.set(key, {'key': key, 'v': version}) is True
                self.owned[key] = version
                got = self.ledger.get(key)
            if not isinstance(got, dict) or got.get('v') != version:
                self.problem(f'{key} read back {got!r} right after writing version {version}')
        else:
            slot = f'once:{int(time.time() // 60)}:{rng.randrange(ONCE_SLOTS_PER_MINUTE)}'
            won = self.ledger.add(slot, self.id)
            with self.lock:
                self.once_tried += 1
                if won:
                    self.once_won.append(slot)

    def record(self, op: str, ns: int) -> None:
        with self.lock:
            samples = self.latency[op]
            if len(samples) < 20_000:
                samples.append(ns)

    def reopen(self) -> None:
        """Close and reopen the chaos handle, as an app restarting a component would."""
        old = self.chaos
        self.chaos = open_chaos(self.run_dir)
        old.close()
        with self.lock:
            self.reopens += 1

    # -- fork test ----------------------------------------------------------------------------

    def fork_test(self) -> None:
        # No set_title() in the child: setproctitle hangs in a child forked from a threaded process.
        pid = os.fork()
        if pid == 0:
            code = 0
            try:
                key = f'own:{self.id}:fork-child'
                assert self.ledger.set(key, {'key': key, 'v': 1}) is True
                assert self.ledger.get(key) == {'key': key, 'v': 1}
                self.chaos.get('c:0')
                self.chaos.set('c:fork', make_value('c:fork', random.Random(), None))
            except BaseException:
                traceback.print_exc()
                code = 1
            os._exit(code)
        deadline = time.monotonic() + FORK_DEADLINE
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                code = os.waitstatus_to_exitcode(status)
                with self.lock:
                    if code == 0:
                        self.forks_ok += 1
                    else:
                        self.forks_failed += 1
                if code != 0:
                    self.problem(f'forked child exited {code}')
                return
            time.sleep(0.05)
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        with self.lock:
            self.forks_failed += 1
        self.problem(f'forked child hung for {FORK_DEADLINE:.0f}s and was killed')

    # -- reporting ----------------------------------------------------------------------------

    def snapshot(self, final: bool = False) -> dict[str, Any]:
        try:
            import psutil

            proc = psutil.Process()
            rss, fds = proc.memory_info().rss, proc.num_fds()
        except Exception:
            import resource

            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            fds = len(os.listdir('/dev/fd'))
        with self.lock:
            latency = {op: summarize(samples) for op, samples in self.latency.items()}
            self.latency = {'get': [], 'set': []}
            snap = {
                'id': self.id,
                'pid': os.getpid(),
                'python': sys.version.split()[0] + ('t' if not sys._is_gil_enabled() else ''),
                'time': time.time(),
                'final': final,
                'ops': self.ops,
                'misses': self.misses,
                'timeouts': self.timeouts,
                'reopens': self.reopens,
                'acked': self.acked,
                'once_tried': self.once_tried,
                'once_won': list(self.once_won),
                'forks_ok': self.forks_ok,
                'forks_failed': self.forks_failed,
                'problem_count': self.problem_count,
                'problems': list(self.problems),
                'latency': latency,
                'rss': rss,
                'fds': fds,
                'wal': wal_stats(self.chaos),
            }
        return snap

    def write_stats(self, final: bool = False) -> None:
        path = self.run_dir / 'stats' / f'{self.id}.json'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.snapshot(final)))
        os.replace(tmp, path)

    def verify_owned(self) -> None:
        for key, version in sorted(self.owned.items()):
            got = self.ledger.get(key)
            if not isinstance(got, dict) or got.get('v') != version:
                self.problem(f'final check: {key} is {got!r}, last acknowledged version {version}')

    def run(self) -> int:
        # Forking with threads running is the point of the fork test.
        warnings.filterwarnings('ignore', r'This process .* is multi-threaded', DeprecationWarning)
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        # The orchestrator sends SIGUSR2 when this worker stalls: every thread's traceback goes to
        # stderr (logs/<id>.log), next to its native `sample` of the process.
        faulthandler.register(signal.SIGUSR2, all_threads=True)
        signal.signal(signal.SIGUSR1, lambda *_: self.fork_requested.set())
        threads = [threading.Thread(target=self.thread_main, args=(n,), daemon=True) for n in range(self.thread_count)]
        for t in threads:
            t.start()
        next_stats = time.monotonic() + STATS_EVERY
        while not self.stop.is_set():
            if self.fork_requested.wait(0.2):
                self.fork_requested.clear()
                self.fork_test()
            if time.monotonic() >= next_stats:
                self.write_stats()
                next_stats += STATS_EVERY
        for t in threads:
            t.join(60)
        self.verify_owned()
        self.write_stats(final=True)
        self.chaos.close()
        self.ledger.close()
        return 0


def wal_stats(cache: Cache) -> dict[str, int]:
    """This process's forced-checkpoint counters for `cache` (absent before D-024: empty)."""
    get = getattr(cache, '_wal_stats', None)
    return dict(get()) if get else {}


def summarize(samples: list[int]) -> dict[str, float] | None:
    if not samples:
        return None
    ordered = sorted(samples)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))] / 1_000  # noqa: E731
    return {'n': len(ordered), 'p50_us': pick(0.50), 'p99_us': pick(0.99), 'max_us': ordered[-1] / 1_000}


def maint(run_dir: Path, op: str) -> int:
    """One maintenance call on the chaos cache; prints `<op> <removed> <seconds>`."""
    cache = open_chaos(run_dir)
    start = time.perf_counter()
    removed = getattr(cache, op)()
    print(f'{op} {removed} {time.perf_counter() - start:.3f}')
    cache.close()
    return 0


def main() -> int:
    from tests.proctitle import set_title

    mode, run_dir = sys.argv[1], Path(sys.argv[2])
    if mode == 'maint':
        set_title(f'soak-maint-{sys.argv[3]}')
        return maint(run_dir, sys.argv[3])
    worker_id, threads, seed = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
    every = int(sys.argv[6]) if len(sys.argv) > 6 else 0
    thread_rate = float(sys.argv[7]) if len(sys.argv) > 7 else 0.0
    set_title(f'soak-worker-{worker_id}')
    assert diskcache_oxide.sqlite_version() == diskcache_oxide.BUNDLED_SQLITE_VERSION
    return Worker(run_dir, worker_id, threads, seed, every, thread_rate).run()


if __name__ == '__main__':
    sys.exit(main())
