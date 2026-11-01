"""A soak worker: one process hammering the chaos and ledger caches from several threads.

Started by soak/soak.py, never by hand:
    python -m soak.worker run <run_dir> <worker_id> <threads> <seed> [<every> <ops/s per thread>]
    python -m soak.worker lanes <run_dir> <worker_id> <seed> <slot>
    python -m soak.worker auto-writer|auto-reader <run_dir> <worker_id> <seed> <store> <stores>
    python -m soak.worker maint <run_dir> clear|expire|cull|checkpoint|verify-held|verify-pq

Two caches share the run directory:
- chaos/: keys shared by every worker, expiring entries, a small size_limit (so culling runs), large
  values that spill to files, and clear()/expire()/cull() from maintenance processes. Anything may
  vanish here; what's checked is that whatever comes back is intact and not expired.
- ledger/: nothing expires, is cleared or is culled, so every acknowledged write must survive:
  - `ctr:<worker>` counters, against the count of incr() calls that returned;
  - `own:<worker>:<n>` keys with a single writer, which must read back exactly what was last written;
  - `once:<slot>` add() races, which must have exactly one winner;
  - `held:<worker>:<n>` keys, a `hctr:<worker>` counter and `htx:<worker>:<t>` manifests, written
    only inside held write transactions that commit or roll back at random. Transaction t counts
    itself in the counter first, stamps t into every value it writes, and lists what it wrote in its
    manifest. After each one, every key it touched must show all of its changes or none, and exactly
    the committed ones. Some values spill to files, so a rollback that loses a committed file, or a
    commit that leaves a replaced one, shows up here or in the orchestrator's file scan. At the end
    of the run, `maint verify-held` checks every worker's keys against the manifests, including
    workers killed mid-transaction: a torn commit can't hide there;
  - `pq:<tick>`, `pqw:<token>` and `delivered:<token>`, the exactly-once lane: every `run` worker's
    threads aim at the same wall-clock tick, race an add() into the slot and race pop() out of it, and
    whoever gets the token records it with add('delivered:<token>'), where a False is a duplicate.
    `maint verify-pq` checks at the end that no recorded token vanished, allowing for kills.

The other roles run lanes that plan 09 needs, on stores of their own:
- `lanes` (LaneWorker): `durable=True` calls with a small timeout on durable/, a fork-behind lane
  (paced traffic with per-call budgets on behind/, forks landing every few seconds), and a real stuck
  fork, a bounded one, in a private stuck/<slot>/. Small timeouts belong here, because the fork gate's
  counters are per process: in the other roles any fork_timeouts count means a fork was stuck.
- `auto-writer`, `auto-reader` (AutoWorker): one writer per auto-<store>/ with
  `wal_checkpoint='auto'`, beside a reader process, in bursts.

Every value carries its key and a CRC of its payload, so corruption or a value landing under the
wrong key is caught on every read.

The worker writes its counters to stats/<worker>.json every few seconds (atomically), and its
problems to the same file, for the orchestrator to collect. SIGUSR1 asks for a fork test (fork
while the threads run; the child must read and write both caches and exit within a deadline).
SIGUSR2 dumps every thread's traceback to stderr (for stall diagnosis). SIGTERM stops it cleanly. It then verifies every owned key, and its final stats carry the exact
acknowledged counter value for the end-of-run check.
"""

from __future__ import annotations

import collections
import faulthandler
import json
import logging
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
from diskcache_oxide import Cache, Timeout, _core

STATS_EVERY = 5.0
CHAOS_KEYS = 20_000
CHAOS_SIZE_LIMIT = 256 * 2**20
OWNED_KEYS = 200
ONCE_SLOTS_PER_MINUTE = 20
LATENCY_SAMPLE_EVERY = 50
MAX_PROBLEMS_KEPT = 50
FORK_DEADLINE = 20.0
# How long, in all, a stopping worker waits for its threads.
THREAD_STOP_LIMIT = 60.0
HELD_KEYS = 50
# The share of ledger operations that run a held transaction instead (a few a second across a run).
HELD_SHARE = 0.01
HELD_COMMIT = 0.6
# A get that started this long after a value's expire time must not return it (the writer's and
# reader's clock reads aren't simultaneous).
EXPIRY_SLACK = 1.0


# Pacing: the shortest sleep worth taking, and how far behind schedule a thread may fall before the
# backlog is dropped (so a stall doesn't turn into a long burst afterwards).
PACE_MIN_SLEEP = 0.005
PACE_MAX_BEHIND = 0.02

# The exactly-once lane: slots per second, and threads per `run` worker. A slot refills once its token
# is popped, so a tick makes several tokens: at --procs 7 --threads 4, about 22 a second, or about 1.9M
# tokens and 4.6M ledger rows (about 630 MiB) in 24 hours. A killed worker can lose at most one token per
# thread, between its pop and its add.
PQ_TICKS_PER_S = 2
PQ_THREADS = 2

# Budgets for the lanes that measure calls. A call that takes longer than its budget by more than
# OVERRUN_ALLOWANCE is a failure. The overshoot the design allows is one poll plus the interpreter
# hand-off, but a commit on a loaded disk isn't bound by `timeout`: the worst get and set in the last
# 24-hour run were 339 ms and 130 ms, and a forced checkpoint's copy has been seen at 1.4 s.
BEHIND_TIMEOUT = 1.0
DURABLE_TIMEOUT = 0.25
OVERRUN_ALLOWANCE = 3.0
LANE_CALLS_PER_S = 200.0
DURABLE_KEYS = 50
FORK_BEHIND_EVERY = (2.0, 4.0)
# The hog holds the durable store's write lock past DURABLE_TIMEOUT, so a durable write really fails.
HOG_EVERY = (5.0, 15.0)
HOG_HOLD = 0.6
# A stuck fork: how often, a caller's timeout behind it and how far past that it may return (the same
# slack as tests/unit/fork_timeout_scenarios.py), and how long the hold waits for that caller before
# giving up and reporting it. A hold never outlasts STUCK_CALLER_LIMIT, so STALL_AFTER keeps its meaning.
STUCK_EVERY = (30.0, 90.0)
STUCK_TIMEOUT = 0.2
STUCK_SLACK = 1.3
STUCK_CALLER_LIMIT = 5.0
# A durable handle logs one line per bucket per 60 s, and a line is emitted after a call that may have
# waited, so two can land a little closer than that.
LOG_MIN_GAP = 55.0

# The auto group. A forced checkpoint needs the WAL over its limit, and measurement says that only
# happens with readers that hold read marks nearly all the time: at 500 writes a second beside paced
# or GIL readers there were none, and at 2,000 a second beside 8 flat-out free-threaded reader threads
# about 25 a second. So readers run flat out in bursts, staggered between stores, and trickle otherwise.
AUTO_WAL_LIMIT = 2**20
AUTO_KEYS = 1_000
AUTO_VALUE = 2_000
AUTO_PERIOD = 30.0
AUTO_BURST = 5.0
AUTO_WRITES_PER_S = 2_000.0
AUTO_IDLE_PER_S = 20.0
AUTO_READER_THREADS = 8
AUTO_READER_SAMPLE = 1_000
AUTO_FORK_EVERY = 7.0


def open_chaos(run_dir: Path) -> Cache:
    return Cache(run_dir / 'chaos', timeout=30.0, size_limit=CHAOS_SIZE_LIMIT)


def open_ledger(run_dir: Path) -> Cache:
    return Cache(run_dir / 'ledger', timeout=60.0, size_limit=2**36)


def pace(interval: float, next_at: float, stop: threading.Event) -> tuple[float, bool]:
    """Waits for this thread's next call at `interval` (see thread_main). Returns the next call's time
    and whether to stop."""
    delay = next_at - time.monotonic()
    if delay > PACE_MIN_SLEEP and stop.wait(delay):
        return next_at, True
    return max(next_at + interval, time.monotonic() - PACE_MAX_BEHIND), False


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
        self.role = 'run'
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
        # The plan 09 lanes' counters, and each lane thread's progress (the orchestrator stalls on it).
        self.c: collections.Counter[str] = collections.Counter()
        self.beats: collections.Counter[str] = collections.Counter()
        self.fork_max_s = 0.0
        self.child_max_s = 0.0
        self.fork_wanted = False
        # Held transactions: one at a time per worker, so `held` (key -> last committed version, 0
        # for deleted) stays exact. Taken without waiting; a thread that finds it busy does a normal
        # ledger op instead.
        self.held_lock = threading.Lock()
        self.held: dict[str, int] = {}
        self.held_acked = 0  # hctr:<id> increments in committed transactions
        self.held_commits = 0
        self.held_rollbacks = 0
        # Set if a check after a transaction failed to read back: `held` may then be stale, so this
        # worker stops holding and leaves the final check to `maint verify-held`.
        self.held_desynced = False

    def problem(self, text: str) -> None:
        with self.lock:
            self.problem_count += 1
            if len(self.problems) < MAX_PROBLEMS_KEPT:
                self.problems.append(f'{time.strftime("%H:%M:%S")} {text}')

    def count(self, name: str, n: int = 1) -> None:
        with self.lock:
            self.c[name] += n

    def beat(self, lane: str) -> None:
        with self.lock:
            self.beats[lane] += 1

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
                # Sleep only once well ahead, then catch up in a burst. Some kernels (OrbStack's
                # Linux VM) round every sleep up to about 3 ms, so sleeping before each 40 us call
                # capped a thread at a few hundred calls a second; the average rate is the same.
                if delay > PACE_MIN_SLEEP and self.stop.wait(delay):
                    break
                next_at = max(next_at + interval, time.monotonic() - PACE_MAX_BEHIND)
            i += 1
            # Often enough that even a very low --rate beats well inside the stall limit.
            if i % 256 == 1 or interval > 0.1:
                self.beat(f'load-{n}')
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
        if i % every == 0 and rng.random() < 0.25:
            asked_at = time.time()
            value = self.chaos.pop(key)
            self.count('pops_chaos')
            if value is not None and (wrong := check_value(key, value, asked_at)) is not None:
                self.problem(f'pop: {wrong}')
            self.ledger_op(rng)
        elif i % every == 0:
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
        if rng.random() < HELD_SHARE and self.held_op(rng):
            return
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

    def held_op(self, rng: random.Random) -> bool:
        """One held write transaction on the ledger, then a check that it committed or rolled back
        as a whole. Returns False, doing nothing, if another thread of this worker is in one."""
        if self.held_desynced or not self.held_lock.acquire(blocking=False):
            return False
        try:
            commit = rng.random() < HELD_COMMIT
            planned: dict[str, int] = {}  # key -> its version if this commits (0: deleted)
            for n in rng.sample(range(HELD_KEYS), rng.randint(1, 4)):
                key = f'held:{self.id}:{n}'
                delete = self.held.get(key, 0) > 0 and rng.random() < 0.2
                planned[key] = 0 if delete else self.held.get(key, 0) + 1
            outcome = 'committed' if commit else 'rolled back'
            # Never take self.lock on the good path inside the hold: ledger_op holds it across a write
            # to this ledger, which waits for this very transaction, so the two would wait for each
            # other until that write's 60 s timeout, with the write lock held all the while.
            pops = 0
            found: list[str] = []  # Problems seen inside the hold, reported after it for the same reason.
            try:
                with self.ledger._hold_write_transaction():
                    tx = self.ledger.incr(f'hctr:{self.id}')
                    for key, version in planned.items():
                        if version:
                            assert self.ledger.set(key, held_value(key, version, tx, rng)) is True
                        elif rng.random() < 0.5:
                            popped = self.ledger.pop(key)
                            pops += 1  # Counted after the transaction: see the note above.
                            if (got := held_version(key, popped)) != self.held.get(key, 0):
                                found.append(f'{key} popped {got} inside a transaction, last committed {self.held.get(key, 0)}')
                        else:
                            self.ledger.delete(key)
                        # Nested operations see the transaction's own writes.
                        if (got := held_version(key, self.ledger.get(key))) != version:
                            found.append(f'{key} read {got} inside its transaction, just after writing {version}')
                    self.ledger.set(f'htx:{self.id}:{tx}', planned)
                    self.chaos.get(f'c:{rng.randrange(CHAOS_KEYS)}')  # A read of another cache, too.
                    if not commit:
                        raise _Rollback
            except _Rollback:
                pass
            except Timeout:
                outcome = 'uncertain'  # Raised before or at COMMIT: all or nothing, but which isn't known.
            except Exception:
                found.append(f'held transaction: {traceback.format_exc(limit=6)}')
                outcome = 'uncertain'
            for text in found:
                self.problem(text)
            try:
                seen = {key: held_version(key, self.ledger.get(key)) for key in planned}
            except Exception:
                self.held_desynced = True  # Not a problem by itself; the final check covers the keys.
                return True
            before = {key: self.held.get(key, 0) for key in planned}
            if outcome == 'uncertain':
                outcome = 'committed' if seen == planned else 'rolled back'
            expected = planned if outcome == 'committed' else before
            if seen != expected:
                self.problem(f'held transaction {outcome}, but its keys read {seen}: expected {expected} (before {before})')
            with self.lock:
                self.c['pops_held'] += pops
                if outcome == 'committed':
                    self.held.update(planned)
                    self.held_acked += 1
                    self.held_commits += 1
                else:
                    self.held_rollbacks += 1
            return True
        finally:
            self.held_lock.release()

    # -- the exactly-once lane ------------------------------------------------------------------

    def pq_main(self, n: int) -> None:
        """Every thread of every `run` worker races for the same slot at the same wall-clock tick, so
        that consumers in different processes really collide on one row. Each thread's token is its
        own: a slot refilled after its first token was taken is a second delivery to make, not a fault."""
        while not self.stop.is_set():
            self.beat(f'pq-{n}')
            now = time.time()
            tick = int(now * PQ_TICKS_PER_S) + 1
            if self.stop.wait(max(0.0, tick / PQ_TICKS_PER_S - now)):
                break
            try:
                token = f'{self.id}.{n}.{tick}'
                if self.ledger.add(f'pq:{tick}', token):
                    self.ledger.set(f'pqw:{token}', tick)
                    self.count('pq_produced')
                # This tick twice, as the winner's add can land after a first miss, then earlier ticks
                # in case every consumer of one was descheduled or killed.
                for slot in (tick, tick, tick - 1, tick - 2, tick - 4):
                    self.pq_consume(f'pq:{slot}')
            except Timeout:
                with self.lock:
                    self.timeouts += 1
            except Exception:
                self.problem(f'exactly-once lane: {traceback.format_exc(limit=6)}')
                time.sleep(0.1)

    def pq_consume(self, slot: str) -> None:
        token = self.ledger.pop(slot)
        self.count('pops_pq')
        if token is None:
            return
        if not isinstance(token, str) or token.count('.') != 2:
            self.problem(f'{slot} popped {token!r}, which no producer wrote')
            return
        self.count('pq_delivered')
        # The pop has taken it: this add must land, or the token is lost, so it outlasts a Timeout.
        while True:
            try:
                first = self.ledger.add(f'delivered:{token}', self.id)
                break
            except Timeout:
                with self.lock:
                    self.timeouts += 1
        if not first:
            self.count('pq_duplicates')
            self.problem(f'duplicate delivery: {slot} gave {token} to worker {self.id}, which another pop had already delivered')

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
        start = time.monotonic()
        pid = os.fork()
        if pid == 0:
            code = 0
            signal.alarm(int(FORK_DEADLINE) * 3)  # If this parent is killed, nothing else ends a hung child.
            try:
                key = f'own:{self.id}:fork-child'
                assert self.ledger.set(key, {'key': key, 'v': 1}) is True
                assert self.ledger.get(key) == {'key': key, 'v': 1}
                self.chaos.get('c:0')
                self.chaos.set('c:fork', make_value('c:fork', random.Random(), None))
                # pop in the child: it gets the value, once. The key is this child's own, since two
                # children of one worker can overlap.
                popped = f'own:{self.id}:fork-pop:{os.getpid()}'
                assert self.ledger.set(popped, {'key': popped, 'v': 1}) is True
                assert self.ledger.pop(popped) == {'key': popped, 'v': 1}
                assert self.ledger.get(popped) is None
                self.child_work()
            except BaseException:
                traceback.print_exc()
                code = 1
            os._exit(code)
        took = time.monotonic() - start
        deadline = time.monotonic() + FORK_DEADLINE
        with self.lock:
            self.fork_max_s = max(self.fork_max_s, took)
        while time.monotonic() < deadline:
            done, status = os.waitpid(pid, os.WNOHANG)
            if done:
                code = os.waitstatus_to_exitcode(status)
                with self.lock:
                    self.child_max_s = max(self.child_max_s, time.monotonic() - start - took)
                    if code == 0:
                        self.forks_ok += 1
                        self.c['pops_fork'] += 1
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
                'held_commits': self.held_commits,
                'held_rollbacks': self.held_rollbacks,
                'problem_count': self.problem_count,
                'problems': list(self.problems),
                'latency': latency,
                'rss': rss,
                'fds': fds,
                'wal': wal_stats(self.chaos),
                'role': self.role,
                'c': dict(self.c),
                'beats': dict(self.beats),
                'gate': _core._gate_stats(),
                'fork_max_s': self.fork_max_s,
                'child_max_s': self.child_max_s,
                **self.extra_stats(),
            }
        return snap

    def extra_stats(self) -> dict[str, Any]:
        return {}

    def child_work(self) -> None:
        """In a forked child: calls on the handles this role's threads are busy on at the fork, as
        opposed to the idle chaos and ledger handles. It has to stay well inside FORK_DEADLINE."""

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
        if self.held_desynced:
            return
        for key, version in sorted(self.held.items()):
            if (got := held_version(key, self.ledger.get(key))) != version:
                self.problem(f'final check: {key} is version {got}, last committed {version}')
        if (count := self.ledger.get(f'hctr:{self.id}', 0)) != self.held_acked:
            self.problem(f'final check: hctr:{self.id} is {count}, committed transactions {self.held_acked}')

    def want_fork(self, *_: Any) -> None:
        self.fork_wanted = True

    def lane_threads(self) -> list[threading.Thread]:
        """The threads this role runs: the load, and the exactly-once lane."""
        load = [threading.Thread(target=self.thread_main, args=(n,), daemon=True) for n in range(self.thread_count)]
        return load + [threading.Thread(target=self.pq_main, args=(n,), daemon=True) for n in range(PQ_THREADS)]

    def close_all(self) -> None:
        self.chaos.close()
        self.ledger.close()

    def run(self) -> int:
        # Forking with threads running is the point of the fork test.
        warnings.filterwarnings('ignore', r'This process .* is multi-threaded', DeprecationWarning)
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        # The orchestrator sends SIGUSR2 when this worker stalls: every thread's traceback goes to
        # stderr (logs/<id>.log), next to its native `sample` of the process.
        faulthandler.register(signal.SIGUSR2, all_threads=True)
        # A plain attribute: an Event's set() in a handler can deadlock with a wait() it interrupted.
        signal.signal(signal.SIGUSR1, self.want_fork)
        threads = self.lane_threads()
        for t in threads:
            t.start()
        next_stats = time.monotonic() + STATS_EVERY
        while not self.stop.is_set():
            time.sleep(0.1)
            if self.fork_wanted:
                self.fork_wanted = False
                self.fork_test()
            if time.monotonic() >= next_stats:
                self.write_stats()
                next_stats += STATS_EVERY
        deadline = time.monotonic() + THREAD_STOP_LIMIT
        for t in threads:
            t.join(max(0.0, deadline - time.monotonic()))
        for t in threads:
            if t.is_alive():
                self.problem(f'{t.name} still running {THREAD_STOP_LIMIT:.0f}s after the stop: a call that hung')
        self.verify_owned()
        self.write_stats(final=True)
        self.close_all()
        return 0


# A pickle hook on an inline value holds a gate level for as long as it runs. What it runs, if anything.
_stuck_hook: Any = None


class Hooked:
    """A value whose decode runs `_stuck_hook`."""

    def __reduce__(self) -> tuple[Any, ...]:
        return (rebuild_hooked, ())


def rebuild_hooked() -> Hooked:
    if _stuck_hook is not None:
        _stuck_hook()
    return Hooked()


class DurableLog(logging.Handler):
    """Counts the `diskcache_oxide` logger's lines by bucket, and fails any that come too soon after
    the last on one store. The lines a `close()` flushes are exempt. Keeps the first few, for the log."""

    KEEP = 3

    def __init__(self, worker: LaneWorker) -> None:
        super().__init__()
        self.worker = worker
        self.lines: collections.Counter[str] = collections.Counter()
        self.last: dict[tuple[str, str], float] = {}
        self.closing = False

    def emit(self, record: logging.LogRecord) -> None:
        text = str(record.msg)
        bucket = 'lost_deletes' if 'lost invalidations' in text else 'busy' if ' busy' in text else 'broken' if ' failing' in text else ''
        if not bucket:
            return  # A decode report, or anything else: not what this checks.
        now = time.monotonic()
        where = (str(record.args[0]) if isinstance(record.args, tuple) and record.args else '', bucket)
        last = self.last.get(where)
        self.last[where] = now
        self.lines[bucket] += 1
        # A broken line always goes to the log: its error text is what decides if it's a real finding.
        if bucket == 'broken' or sum(self.lines.values()) <= self.KEEP:
            print(record.getMessage(), file=sys.stderr, flush=True)
        if last is not None and now - last < LOG_MIN_GAP and not self.closing:
            self.worker.problem(f'durable log: two {bucket} lines for one store {now - last:.1f}s apart, under the 60 s limit')


class LaneWorker(Worker):
    """The lanes that need small timeouts: durable calls, forks behind traffic, stuck forks."""

    def __init__(self, run_dir: Path, worker_id: int, seed: int, slot: int) -> None:
        super().__init__(run_dir, worker_id, 0, seed)
        self.role = 'lanes'
        self.durable = Cache(run_dir / 'durable', timeout=DURABLE_TIMEOUT, durable=True, size_limit=2**34)
        self.truth = Cache(run_dir / 'durable', timeout=60.0, size_limit=2**34)  # The non-durable view of it.
        self.hog = Cache(run_dir / 'durable', timeout=5.0, size_limit=2**34)
        self.behind = Cache(run_dir / 'behind', timeout=BEHIND_TIMEOUT, size_limit=2**34)
        private = run_dir / 'stuck' / str(slot)
        self.stuck_held = Cache(private / 'held', timeout=30.0, pickle=True)
        self.stuck_call = Cache(private / 'call', timeout=STUCK_TIMEOUT)
        self.stuck_held.set('hooked', Hooked())
        self.calls_started = 0
        self.max_overrun = 0.0
        self.stuck_caller_max_s = 0.0
        self.durable_acked: dict[int, dict[str, int]] = {}
        self.counted: dict[int, int] = {}
        self.log = DurableLog(self)
        logger = logging.getLogger('diskcache_oxide')
        logger.addHandler(self.log)
        logger.setLevel(logging.WARNING)
        logger.propagate = False

    def lane_threads(self) -> list[threading.Thread]:
        spawn = lambda target, *args: threading.Thread(target=target, args=args, daemon=True)  # noqa: E731
        return [
            *(spawn(self.behind_main, n) for n in range(2)),
            *(spawn(self.durable_main, n) for n in range(2)),
            spawn(self.hog_main),
            spawn(self.forks_main),
            spawn(self.stuck_main),
        ]

    def timed(self, what: str, budget: float, work: Any) -> Any:
        """`work()`, failing the run if it overruns `budget` by more than OVERRUN_ALLOWANCE."""
        with self.lock:
            self.calls_started += 1
            self.ops += 1
        start = time.monotonic()
        try:
            return work()
        finally:
            over = time.monotonic() - start - budget
            with self.lock:
                self.max_overrun = max(self.max_overrun, over)
            if over > OVERRUN_ALLOWANCE:
                self.count('budget_overruns')
                self.problem(f'{what} took {over + budget:.2f}s against a budget of {budget}s (allowance {OVERRUN_ALLOWANCE}s)')

    def child_work(self) -> None:
        rng = random.Random()
        key = 'b:fork-child'
        try:
            self.behind.set(key, make_value(key, rng, None))
            if (value := self.behind.get(key)) is not None and (wrong := check_value(key, value, time.time())) is not None:
                raise AssertionError(wrong)
        except Timeout:
            pass  # A budget of 1 s can run out beside a peer. The hung-child check covers a real hang.
        key = 'dur:fork-child'
        self.durable.set(key, durable_value(key, 1, rng))  # Answers an outage: nothing to catch.
        self.durable.get(key)

    # -- forks behind traffic ------------------------------------------------------------------

    def behind_main(self, n: int) -> None:
        rng = random.Random(self.seed * 1_000 + 10 + n)
        interval = 1.0 / LANE_CALLS_PER_S
        next_at = time.monotonic()
        while not self.stop.is_set():
            self.beat(f'behind-{n}')
            next_at, stopping = pace(interval, next_at, self.stop)
            if stopping:
                break
            key = f'b:{rng.randrange(200)}'
            roll = rng.random()
            try:
                if roll < 0.7:
                    value = self.timed('behind get', BEHIND_TIMEOUT, lambda: self.behind.get(key))
                elif roll < 0.9:
                    payload = make_value(key, rng, None)
                    self.timed('behind set', BEHIND_TIMEOUT, lambda: self.behind.set(key, payload))
                    value = None
                else:
                    value = self.timed('behind pop', BEHIND_TIMEOUT, lambda: self.behind.pop(key))
                self.count('behind_calls')
                if value is not None and (wrong := check_value(key, value, time.time())) is not None:
                    self.problem(f'behind: {wrong}')
            except Timeout:
                self.count('behind_calls')
                self.count('behind_timeouts')
            except Exception:
                self.problem(f'behind lane: {traceback.format_exc(limit=6)}')
                time.sleep(0.1)

    def forks_main(self) -> None:
        """Forks landing during that traffic. One counts as behind traffic if a call started while it
        was going on, so a lane whose traffic stopped can't count any."""
        rng = random.Random(self.seed * 1_000 + 20)
        while not self.stop.wait(rng.uniform(*FORK_BEHIND_EVERY)):
            self.beat('forks')
            before = self.calls_started
            self.fork_test()
            self.count('forks_tried')
            if self.calls_started != before:
                self.count('forks_behind')

    # -- a stuck fork --------------------------------------------------------------------------

    def stuck_main(self) -> None:
        """One thread keeps a level (a held transaction, or a pickle hook on an inline value) while
        another forks, which can't finish until it lets go, and a third calls a cache: that call must
        raise Timeout at its bound. The level then goes, and the fork must complete."""
        global _stuck_hook
        rng = random.Random(self.seed * 1_000 + 30)
        calls = ('get', 'set', 'pop', 'in')
        n = 0
        while True:
            pause = rng.uniform(*STUCK_EVERY)
            end = time.monotonic() + pause
            while time.monotonic() < end:
                self.beat('stuck')
                if self.stop.wait(min(1.0, pause)):
                    return
            n += 1
            forked = threading.Event()
            released = threading.Event()

            # n // 2, so that every call meets both the hold and the hook.
            def scene(call: str = calls[(n // 2) % len(calls)]) -> None:
                released.clear()
                threading.Thread(target=self.stuck_fork, args=(forked, released), daemon=True).start()
                self.stuck_scene(call, forked)
                released.set()

            try:
                if n % 2:
                    with self.stuck_held._hold_write_transaction():
                        scene()
                else:
                    _stuck_hook = scene
                    try:
                        self.stuck_held.get('hooked')
                    finally:
                        _stuck_hook = None
            except Timeout:
                self.count('stuck_begin_timeouts')  # Beginning the hold, behind another of this process's forks.
                continue
            except Exception:
                self.problem(f'stuck fork: {traceback.format_exc(limit=6)}')
                continue
            finally:
                released.set()
            self.count('stuck_forks')
            if forked.wait(FORK_DEADLINE * 2):
                self.count('stuck_forks_completed')
            else:
                self.problem(f'a stuck fork did not finish within {FORK_DEADLINE * 2:.0f}s of its level going')

    def stuck_fork(self, forked: threading.Event, released: threading.Event) -> None:
        self.fork_test()
        if not released.is_set():
            self.problem('a fork returned while a thread still held a gate level: it did not quiesce')
        forked.set()

    def stuck_scene(self, call: str, forked: threading.Event) -> None:
        """Runs while this thread holds a level, for at most about STUCK_CALLER_LIMIT * 2 seconds."""
        deadline = time.monotonic() + STUCK_CALLER_LIMIT
        while not _core._gate_stats()['pending']:
            if forked.is_set() or time.monotonic() > deadline:
                self.problem('the stuck fork never claimed the gate')
                return
            time.sleep(0.001)
        box: list[tuple[Any, float]] = []

        def caller() -> None:
            handle = self.stuck_call
            work = {
                'get': lambda: handle.get('k'),
                'set': lambda: handle.set('k', 1),
                'pop': lambda: handle.pop('k'),
                'in': lambda: 'k' in handle,
            }[call]
            start = time.monotonic()
            try:
                result = work()
            except BaseException as ex:
                result = ex
            box.append((result, time.monotonic() - start))

        thread = threading.Thread(target=caller, daemon=True)
        thread.start()
        thread.join(STUCK_CALLER_LIMIT)
        if not box:
            self.count('stuck_callers_hung')
            self.problem(f'{call} behind a stuck fork had not returned after {STUCK_CALLER_LIMIT}s (timeout {STUCK_TIMEOUT}s): the wedge')
            return
        result, took = box[0]
        with self.lock:
            self.stuck_caller_max_s = max(self.stuck_caller_max_s, took)
        if not isinstance(result, Timeout):
            self.problem(f'{call} behind a stuck fork returned {result!r} after {took:.3f}s, not Timeout')
        elif not STUCK_TIMEOUT * 0.95 <= took < STUCK_TIMEOUT + STUCK_SLACK:
            self.problem(f'{call} behind a stuck fork timed out after {took:.3f}s (timeout {STUCK_TIMEOUT}s)')
        else:
            self.count('stuck_callers_timed_out')

    # -- durable ------------------------------------------------------------------------------

    def hog_main(self) -> None:
        rng = random.Random(self.seed * 1_000 + 40)
        while not self.stop.wait(rng.uniform(*HOG_EVERY)):
            self.beat('hog')
            try:
                with self.hog._hold_write_transaction():
                    self.count('hog_holds')
                    self.stop.wait(HOG_HOLD)
            except Timeout:
                self.count('hog_timeouts')
            except Exception:
                self.problem(f'hog: {traceback.format_exc(limit=6)}')

    def durable_call(self, name: str, work: Any) -> Any:
        """A durable call: nothing operational may escape it, and it must keep to its budget.
        Returns _ESCAPED if something did, which is already a failure."""
        try:
            result = self.timed(f'durable {name}', DURABLE_TIMEOUT, work)
        except Exception as ex:
            self.count('durable_escapes')
            self.problem(f'durable {name} let {ex!r} escape')
            return _ESCAPED
        self.count('durable_calls')
        return result

    def truth_version(self, key: str) -> int | str | None:
        try:
            return held_version(key, self.truth.get(key))
        except Timeout:
            self.count('truth_timeouts')
            return None

    def durable_main(self, n: int) -> None:
        rng = random.Random(self.seed * 1_000 + 50 + n)
        acked: dict[str, int] = {}  # key -> the last version acknowledged (0: not there). Only this thread writes these keys.
        self.durable_acked[n] = acked
        self.counted[n] = 0
        interval = 1.0 / LANE_CALLS_PER_S
        next_at = time.monotonic()
        i = 0
        while not self.stop.is_set():
            self.beat(f'durable-{n}')
            next_at, stopping = pace(interval, next_at, self.stop)
            if stopping:
                break
            i += 1
            try:
                self.durable_op(rng, n, acked)
                if i % 1_000 == 1:
                    self.bugs_still_raise()
            except Exception:
                self.problem(f'durable lane: {traceback.format_exc(limit=6)}')
                time.sleep(0.1)

    def durable_op(self, rng: random.Random, n: int, acked: dict[str, int]) -> None:
        key = f'dur:{self.id}:{n}:{rng.randrange(DURABLE_KEYS)}'
        have = acked.get(key, 0)
        d = self.durable
        roll = rng.random()

        def still(after: str, want: int) -> None:
            """A call answered as a failure changed nothing: the read-back is what was acknowledged."""
            self.count('durable_false')
            if (got := self.truth_version(key)) not in (want, None):
                self.problem(f'durable {after} returned a failure for {key}, but it reads version {got}, not the previous {want}')

        if roll < 0.40:
            got = self.durable_call('get', lambda: d.get(key, _MISS))
            if got is _MISS and have:
                self.count('durable_misses')
            elif got is not _MISS and got is not _ESCAPED and (version := held_version(key, got)) != have:
                self.problem(f'durable get of {key} read {version}, last acknowledged {have}')
        elif roll < 0.55:
            got = self.durable_call('in', lambda: key in d)
            if got is True and not have:
                self.problem(f'{key} in a durable handle is True, but it was never written or was removed')
            elif got is False and have:
                self.count('durable_misses')
        elif roll < 0.80:
            version = have + 1
            value = durable_value(key, version, rng)
            got = self.durable_call('set', lambda: d.set(key, value))
            if got is True:
                acked[key] = version
                self.count('durable_set_ok')
                if rng.random() < 0.1 and (read := self.truth_version(key)) not in (version, None):
                    self.problem(f'durable set of {key} returned True, but it reads {read}, not {version}')
            elif got is False:
                self.counted[n] += 1
                still('set', have)
        elif roll < 0.85:
            value = durable_value(key, 1, rng)
            got = self.durable_call('add', lambda: d.add(key, value))
            if got is True:
                if have:
                    self.problem(f'durable add of {key} won, but version {have} is there')
                acked[key] = 1
            elif got is False and not have:
                still('add', 0)
        elif roll < 0.90:
            got = self.durable_call('pop', lambda: d.pop(key, _MISS))
            if got is _MISS and have:
                still('pop', have)  # The default, never the value, with the row kept.
            elif got is not _MISS and got is not _ESCAPED:
                if (version := held_version(key, got)) != have:
                    self.problem(f'durable pop of {key} gave {version}, last acknowledged {have}')
                acked[key] = 0
        elif roll < 0.93:
            got = self.durable_call('delete', lambda: d.delete(key))
            if got is True:
                if not have:
                    self.problem(f'durable delete of {key} removed something, but nothing was acknowledged there')
                acked[key] = 0
            elif got is False and have:
                still('delete', have)
        elif roll < 0.96:
            got = self.durable_call('touch', lambda: d.touch(key))
            if got is False and have:
                still('touch', have)
            elif got is True and not have:
                self.problem(f'durable touch of {key} found a key that was never written or was removed')
        else:
            counter = f'durctr:{self.id}:{n}'
            self.durable_call('incr', lambda: d.incr(counter))
            self.durable_call('decr', lambda: d.decr(counter))

    def bugs_still_raise(self) -> None:
        """A bug isn't an outage: these raise whatever the store is doing, before it's reached."""
        self.count('durable_bug_checks')
        for what, work in (('a non-str key to set', lambda: self.durable.set(123, 'v')), ('a non-str key to get', lambda: self.durable.get(123))):  # ty: ignore[arg-type]
            try:
                work()
            except TypeError:
                continue
            self.count('durable_bugs_swallowed')
            self.problem(f'{what} on a durable handle did not raise TypeError')

    def verify_owned(self) -> None:
        super().verify_owned()
        for acked in self.durable_acked.values():
            for key, version in sorted(acked.items()):
                if (got := self.truth_version(key)) not in (version, None):
                    self.problem(f'final check: {key} is version {got}, last acknowledged {version}')
        # Every set answered False was counted as a failure, and the other way about.
        stats = self.durable._durable_stats()
        counted = stats['busy']['set'] + stats['broken']['set']
        answered = sum(self.counted.values())
        if counted != answered:
            self.problem(f'final check: {answered} durable set calls returned False, but the handle counted {counted} failures')

    def extra_stats(self) -> dict[str, Any]:
        lines = dict(self.log.lines)
        return {'durable': self.durable._durable_stats(), 'log_lines': lines, 'max_overrun': self.max_overrun, 'stuck_caller_max_s': self.stuck_caller_max_s}

    def close_all(self) -> None:
        self.log.closing = True
        for cache in (self.durable, self.truth, self.hog, self.behind, self.stuck_held, self.stuck_call):
            cache.close()
        super().close_all()


_MISS = object()
_ESCAPED = object()


def durable_value(key: str, version: int, rng: random.Random) -> Any:
    """A durable lane's value, in held_parse's format: mostly small, sometimes a file."""
    if rng.random() < 0.95:
        return {'key': key, 'v': version, 'tx': 0}
    payload = rng.randbytes(rng.randrange(70 * 1024, 200 * 1024))
    header = json.dumps({'key': key, 'v': version, 'tx': 0, 'crc': zlib.crc32(payload)}).encode()
    return len(header).to_bytes(4, 'big') + header + payload


class AutoWorker(Worker):
    """One side of an auto store: its writer (one process, one thread) or a reader process."""

    def __init__(self, run_dir: Path, worker_id: int, seed: int, role: str, store: int, stores: int) -> None:
        super().__init__(run_dir, worker_id, 0, seed)
        self.role = role
        self.store = store
        self.phase = AUTO_PERIOD * store / max(1, stores)  # Staggered, so stores burst in turn.
        self.auto = Cache(run_dir / f'auto-{store}', timeout=30.0, size_limit=2**34, wal_size_limit=AUTO_WAL_LIMIT, wal_checkpoint='auto')
        self.wal_path = run_dir / f'auto-{store}' / 'diskcache-oxide.sqlite-wal'
        self.peak_wal = 0

    def bursting(self) -> bool:
        return (time.time() + self.phase) % AUTO_PERIOD < AUTO_BURST

    def lane_threads(self) -> list[threading.Thread]:
        if self.role == 'auto-reader':
            return [threading.Thread(target=self.reader_main, args=(n,), daemon=True) for n in range(AUTO_READER_THREADS)]
        return [threading.Thread(target=self.writer_main, daemon=True), threading.Thread(target=self.forks_main, daemon=True)]

    def child_work(self) -> None:
        key = 'a:fork-child'
        self.auto.set(key, {'key': key, 'crc': zlib.crc32(b''), 'body': '', 'exp': None})
        if (value := self.auto.get(key)) is not None and (wrong := check_value(key, value, time.time())) is not None:
            raise AssertionError(wrong)

    def writer_main(self) -> None:
        rng = random.Random(self.seed * 1_000)
        body = 'x' * AUTO_VALUE
        crc = zlib.crc32(body.encode())
        next_at = time.monotonic()
        n = 0
        while not self.stop.is_set():
            self.beat('writer')
            interval = 1.0 / (AUTO_WRITES_PER_S if self.bursting() else AUTO_IDLE_PER_S)
            next_at, stopping = pace(interval, next_at, self.stop)
            if stopping:
                break
            n += 1
            key = f'a:{rng.randrange(AUTO_KEYS)}'
            try:
                self.auto.set(key, {'key': key, 'crc': crc, 'body': body, 'exp': None})
                self.count('auto_writes')
                with self.lock:
                    self.ops += 1
                if n % 50 == 0:
                    self.note_wal()
            except Timeout:
                with self.lock:
                    self.timeouts += 1
            except Exception:
                self.problem(f'auto writer: {traceback.format_exc(limit=6)}')
                time.sleep(0.1)

    def note_wal(self) -> None:
        try:
            size = self.wal_path.stat().st_size
        except FileNotFoundError:
            return
        with self.lock:
            self.peak_wal = max(self.peak_wal, size)

    def forks_main(self) -> None:
        # Forks beside the rounds, so `fork_pending` means something.
        while not self.stop.wait(AUTO_FORK_EVERY):
            self.beat('forks')
            self.fork_test()

    def reader_main(self, n: int) -> None:
        next_at = time.monotonic()
        i = 0
        while not self.stop.is_set():
            bursting = self.bursting()
            if not bursting:  # Flat out in a burst: that's what holds the read marks.
                next_at, stopping = pace(1.0 / AUTO_IDLE_PER_S, next_at, self.stop)
                if stopping:
                    break
            i += 1
            # Only a sample of the bookkeeping, and of the CRC: time spent here isn't in a read.
            sampled = not bursting or i % AUTO_READER_SAMPLE == 0
            if sampled:
                self.beat(f'reader-{n}')
                with self.lock:
                    self.ops += 1 if not bursting else AUTO_READER_SAMPLE
            key = f'a:{i % AUTO_KEYS}'
            try:
                value = self.auto.get(key)
                if sampled and value is not None and (wrong := check_value(key, value, time.time())) is not None:
                    self.problem(f'auto reader: {wrong}')
            except Timeout:
                with self.lock:
                    self.timeouts += 1
            except Exception:
                self.problem(f'auto reader: {traceback.format_exc(limit=6)}')
                time.sleep(0.1)

    def extra_stats(self) -> dict[str, Any]:
        if self.role != 'auto-writer':
            return {'store': self.store}
        return {'store': self.store, 'auto': {**wal_stats(self.auto), 'peak_wal': self.peak_wal, 'limit': AUTO_WAL_LIMIT}}

    def close_all(self) -> None:
        self.auto.close()
        super().close_all()


class _Rollback(Exception):
    """Raised inside a held transaction to roll it back."""


def held_value(key: str, version: int, tx: int, rng: random.Random) -> Any:
    """A held-transaction value written by transaction `tx`: a small dict, or (30%) bytes large
    enough to spill to a file."""
    if rng.random() < 0.7:
        return {'key': key, 'v': version, 'tx': tx}
    payload = rng.randbytes(rng.randrange(70 * 1024, 200 * 1024))
    header = json.dumps({'key': key, 'v': version, 'tx': tx, 'crc': zlib.crc32(payload)}).encode()
    return len(header).to_bytes(4, 'big') + header + payload


def held_parse(key: str, value: Any) -> tuple[int, int] | str:
    """(version, transaction) from a held-transaction value ((0, 0) if missing), or what's wrong."""
    if value is None:
        return (0, 0)
    if isinstance(value, dict):
        header = value
    else:
        n = int.from_bytes(value[:4], 'big')
        header = json.loads(value[4 : 4 + n])
        if zlib.crc32(value[4 + n :]) != header.get('crc'):
            return f'version {header.get("v")} fails its CRC'
    if header.get('key') != key:
        return f'belongs to {header.get("key")!r}'
    if 'tx' not in header:
        return f'version {header.get("v")} has no transaction stamp'
    return (int(header['v']), int(header['tx']))


def held_version(key: str, value: Any) -> int | str:
    """The version a held-transaction value carries (0 if missing), or what's wrong with it."""
    parsed = held_parse(key, value)
    return parsed if isinstance(parsed, str) else parsed[0]


def verify_held(run_dir: Path) -> list[str]:
    """Every worker's held keys against its transactions' manifests, after the run (killed workers
    included): the counter counts exactly the manifests, each key holds what the last manifest that
    lists it wrote, stamped with that transaction, and no value comes from a transaction with no
    manifest (a torn or rolled-back commit)."""
    ledger = open_ledger(run_dir)
    manifests: dict[int, dict[int, dict[str, int]]] = {}
    for key in ledger.iterkeys(prefix='htx:'):
        _, wid, tx = key.split(':')
        manifests.setdefault(int(wid), {})[int(tx)] = ledger.get(key)
    values: dict[int, dict[str, tuple[int, int] | str]] = {}
    for key in ledger.iterkeys(prefix='held:'):
        values.setdefault(int(key.split(':')[1]), {})[key] = held_parse(key, ledger.get(key))
    problems = []
    for wid in sorted(set(manifests) | set(values)):
        txs = manifests.get(wid, {})
        count = ledger.get(f'hctr:{wid}', 0)
        if sorted(txs) != list(range(1, count + 1)):
            problems.append(f'worker {wid}: hctr is {count}, but it has {len(txs)} manifests (highest {max(txs, default=0)})')
        last: dict[str, tuple[int, int]] = {}
        for tx in sorted(txs):
            for key, version in txs[tx].items():
                last[key] = (version, tx)
        got = values.get(wid, {})
        for key, (version, tx) in sorted(last.items()):
            found = got.get(key, (0, 0))
            want = (0, 0) if version == 0 else (version, tx)
            if found != want:
                problems.append(f'{key} is {found} (version, transaction); its last commit, {tx}, wrote {want}')
        problems.extend(f'{key} is {found}, but no committed transaction wrote it' for key, found in sorted(got.items()) if key not in last)
    ledger.close()
    return problems


def verify_pq(run_dir: Path) -> dict[str, Any]:
    """The exactly-once lane's end state: of the tokens a producer recorded winning, which are neither
    delivered nor still queued (lost, each one a failure unless a kill explains it). Each add() said if
    a delivery was a duplicate, as it happened."""
    ledger = open_ledger(run_dir)
    produced = {key.removeprefix('pqw:') for key in ledger.iterkeys(prefix='pqw:')}
    delivered = {key.removeprefix('delivered:') for key in ledger.iterkeys(prefix='delivered:')}
    queued = {ledger.get(key) for key in ledger.iterkeys(prefix='pq:')}
    queued.discard(None)
    ledger.close()
    return {
        'produced': len(produced),
        'delivered': len(delivered),
        'queued': len(queued),
        'lost': sorted(produced - delivered - queued),
        'delivered_and_queued': sorted(delivered & queued),
        # A kill between a winning add and its record leaves a token with no record: harmless.
        'unrecorded': len((delivered | queued) - produced),
        'ticks_per_s': PQ_TICKS_PER_S,
        'threads': PQ_THREADS,
    }


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
    """One maintenance call on the chaos cache; prints `<op> <removed> <seconds>`. Or, for
    `verify-held`, the held-transaction check: one `problem: ...` line each, then a summary."""
    if op == 'verify-held':
        start = time.perf_counter()
        problems = verify_held(run_dir)
        for text in problems:
            print(f'problem: {text}')
        print(f'verify-held {len(problems)} {time.perf_counter() - start:.3f}')
        return 0
    if op == 'verify-pq':
        print(json.dumps(verify_pq(run_dir)))
        return 0
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
    if mode == 'lanes':
        worker_id, seed, slot = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
        set_title(f'soak-lanes-{worker_id}')
        return LaneWorker(run_dir, worker_id, seed, slot).run()
    if mode in ('auto-writer', 'auto-reader'):
        worker_id, seed, store, stores = (int(x) for x in sys.argv[3:7])
        set_title(f'soak-{mode}-{worker_id}')
        return AutoWorker(run_dir, worker_id, seed, mode, store, stores).run()
    worker_id, threads, seed = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
    every = int(sys.argv[6]) if len(sys.argv) > 6 else 0
    thread_rate = float(sys.argv[7]) if len(sys.argv) > 7 else 0.0
    set_title(f'soak-worker-{worker_id}')
    assert diskcache_oxide.sqlite_version() == diskcache_oxide.BUNDLED_SQLITE_VERSION
    return Worker(run_dir, worker_id, threads, seed, every, thread_rate).run()


if __name__ == '__main__':
    sys.exit(main())
