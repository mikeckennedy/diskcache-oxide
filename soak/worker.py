"""A soak worker: one process hammering the chaos and ledger caches from several threads.

Started by soak/soak.py, never by hand:
    python -m soak.worker run <run_dir> <worker_id> <threads> <seed> [<every> <ops/s per thread>]
    python -m soak.worker lanes <run_dir> <worker_id> <seed> <slot>
    python -m soak.worker auto-writer|auto-reader <run_dir> <worker_id> <seed> <store> <stores>
    python -m soak.worker fanout <run_dir> <worker_id> <seed> <threads>
    python -m soak.worker maint <run_dir> clear|expire|cull|checkpoint|verify-held|verify-pq

Two caches share the run directory:
- chaos/: keys shared by every worker, expiring entries, a small size_limit (so culling runs), large
  values that spill to files, and clear()/expire()/cull() from maintenance processes. Anything may
  vanish here; what's checked is that whatever comes back is intact and not expired.
- ledger/: nothing expires or is cleared, and it is under `eviction_policy='none'` far past its
  1 MiB `size_limit`, so nothing is ever evicted and every acknowledged write must survive (a
  `maint cull` culls it too, and must remove nothing):
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
    Each round also push()es a token onto the shared queue `pqueue` and pull()s one from its front,
    delivered and checked the same way;
    `maint verify-pq` checks at the end that no recorded token vanished, allowing for kills.

The other roles run lanes that plan 09 needs, on stores of their own:
- `lanes` (LaneWorker): `durable=True` calls with a small timeout on durable/, a fork-behind lane
  (paced traffic with per-call budgets on behind/, forks landing every few seconds), and a real stuck
  fork, a bounded one, in a private stuck/<slot>/. Small timeouts belong here, because the fork gate's
  counters are per process: in the other roles any fork_timeouts count means a fork was stuck.
- `auto-writer`, `auto-reader` (AutoWorker): one writer per auto-<store>/ with
  `wal_checkpoint='auto'`, beside a reader process, in bursts.
- `fanout` (FanoutWorker): a FanoutCache with 8 shards on fanout/, shared by every fanout process:
  paced routed calls from several threads (self-checking values, some spilled, some expiring), a
  `fctr:<worker>` counter and `fown:<worker>:<n>` keys that must survive, sorted merges and
  `__iter__` walks that must repeat and lose nothing, fanned-out `expire`/`cull`/`checkpoint` under
  one timeout, forks while all of that runs (the child writes every shard), and a check that its
  database fds stay within the shard budget (D-037).

Every value carries its key and a CRC of its payload, so corruption or a value landing under the
wrong key is caught on every read. It carries the tag it was stored with too: chaos/ writes tag most
entries with one of a few groups (plan 24, release 3), a share of reads and every pop ask for the tag
and check it is the one stored with that value (never another version's), and now and then a write
is followed by `evict` of a group.

The worker writes its counters to stats/<worker>.json every few seconds (atomically), and its
problems to the same file, for the orchestrator to collect. SIGUSR1 asks for a fork test (fork
while the threads run; the child must read and write both caches and exit within a deadline).
SIGUSR2 dumps every thread's traceback to stderr (for stall diagnosis). SIGTERM stops it cleanly. It then verifies
every owned key, and its final stats carry the exact
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
import subprocess
import sys
import threading
import time
import traceback
import warnings
import zlib
from pathlib import Path
from typing import Any, cast

import diskcache_oxide
from diskcache_oxide import Cache, FanoutCache, Timeout, _core

# The soak needs fork, SIGKILL, SIGUSR1/2 and the load average: Unix only (plan 25 leaves its port for later).
assert sys.platform != 'win32'

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
# reader's clock reads aren't simultaneous). Used only by reads that don't ask for the cache's own
# expire time.
EXPIRY_SLACK = 1.0
# The wall clock's offset from the monotonic clock when the worker started (see `problem`).
CLOCK_OFFSET_AT_START = time.time() - time.monotonic()
# With the cache's own expire time there's no race: the cache reads the clock when the call starts,
# after `asked_at`, so a value whose stored expiry had passed when the get began must never come back.
# This covers float rounding only. A failure message then says which expiry was passed, the stored one
# or the writer's, so a stale read and a writer whose expiry disagrees with the cache's are told apart.
STORED_EXPIRY_SLACK = 0.01


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
# The queue that every `run` worker's exactly-once threads also push to and pull from, on the ledger.
QUEUE_PREFIX = 'pqueue'

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
# A timed call still running after SLOW_SAMPLE_AFTER gets its process sampled while it's slow: every
# Python thread's traceback and a native `sample` (macOS only) into stalls/, so an overrun comes with
# where the time went. At most one per worker per SLOW_SAMPLE_GAP. A sample pauses the process for
# microseconds at a time; it locates an overrun, it never excuses one.
SLOW_SAMPLE_AFTER = 1.5
SLOW_SAMPLE_GAP = 600.0
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
# The fanout role (FanoutWorker): a FanoutCache on fanout/ at the default 8 shards and connection
# budget, so its database fds stay within 2 x max_connections + 2 x shards (D-037). Paced, so it
# shares the machine with the `run` workers.
FANOUT_SHARDS = 8
FANOUT_FD_BUDGET = 2 * (2 * FANOUT_SHARDS) + 2 * FANOUT_SHARDS
FANOUT_SIZE_LIMIT = 2**34
FANOUT_KEYS = 5_000
FANOUT_OWNED = 200
FANOUT_CALLS_PER_S = 500.0
FANOUT_WALK_EVERY = 5.0
FANOUT_MAINT_EVERY = 30.0
FANOUT_MAINT_TIMEOUT = 10.0
FANOUT_REOPEN_EVERY = 60  # walks, so about every 5 minutes
FANOUT_FORK_EVERY = 10.0


def open_chaos(run_dir: Path) -> Cache:
    return Cache(run_dir / 'chaos', timeout=30.0, size_limit=CHAOS_SIZE_LIMIT)


def open_ledger(run_dir: Path) -> Cache:
    # Under 'none' with the smallest size_limit, which it outgrows in minutes (plan 24, release 3):
    # every write is over the limit, so one that evicted for size would lose an acknowledged write,
    # and the ledger's checks would say so.
    return Cache(run_dir / 'ledger', timeout=60.0, size_limit=2**20, eviction_policy='none')


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


def key_tag(key: Any) -> str:
    """How a value records the key it was stored under: the key itself for a `str`, else its repr."""
    return key if isinstance(key, str) else repr(key)


def soak_key(rng: random.Random, prefix: str, n: int) -> Any:
    """A key for slot `n`: mostly `str`, otherwise a typed key (plan 24), so every lane meets both."""
    form = rng.random()
    if form < 0.70:
        return f'{prefix}:{n}'
    if form < 0.80:
        return n
    if form < 0.90:
        return (prefix, n, None)  # memoize's shape
    if form < 0.95:
        return f'{prefix}:{n}'.encode()
    return (prefix, n + 0.5)


# The tags chaos/ writes use: a few groups of different types (an int and a float that are equal in
# Python are different tags), or none.
SOAK_TAGS: tuple[Any, ...] = ('g:0', 'g:1', 'g:2', 3, 3.0, ('g', 4))
UNCHECKED = object()


def soak_tag(rng: random.Random) -> Any:
    """A tag for a chaos/ write: none a third of the time, else one of `SOAK_TAGS`."""
    return None if rng.random() < 0.33 else rng.choice(SOAK_TAGS)


def make_value(key: Any, rng: random.Random, expire_at: float | None, stored_tag: Any = None) -> Any:
    tag = key_tag(key)
    roll = rng.random()
    if roll < 0.90:
        body = 'x' * rng.randrange(10, 400)
        return {
            'key': tag,
            'crc': zlib.crc32(body.encode()),
            'body': body,
            'exp': expire_at,
            'n': rng.randrange(1 << 40),
            'tag': repr(stored_tag),
        }
    size = rng.randrange(1_024, 8_192) if roll < 0.99 else rng.randrange(64 * 1024, 512 * 1024)
    payload = rng.randbytes(size)
    header = json.dumps({'key': tag, 'crc': zlib.crc32(payload), 'exp': expire_at, 'tag': repr(stored_tag)}).encode()
    return len(header).to_bytes(4, 'big') + header + payload


def check_value(
    key: Any, value: Any, now: float, stored_tag: Any = UNCHECKED, stored_expire: Any = UNCHECKED
) -> str | None:
    """None if `value` is intact and current (and, if `stored_tag` is given, was stored with that
    tag), else what's wrong with it.

    `stored_expire` is the expire time the cache returned with the value (`expire_time=True`). When
    it's given, currency is judged against it, and it must agree with what the writer recorded: no
    expiry for none, and never earlier than the writer's (read just before its set began). It may be
    later by any amount: the cache reads the clock when its call starts, and a writer thread can be
    descheduled between recording the time and calling set() (1.3 s seen on the loaded mini,
    2026-11-11), so a later stored expiry says nothing about the library.
    """
    tag = key_tag(key)
    if isinstance(value, dict):
        if value.get('key') != tag:
            return f'value under {key!r} belongs to {value.get("key")!r}'
        if zlib.crc32(str(value.get('body', '')).encode()) != value.get('crc'):
            return f'dict value under {key!r} fails its CRC'
        expire_at = value.get('exp')
        recorded = value.get('tag', 'None')
    elif isinstance(value, bytes):
        n = int.from_bytes(value[:4], 'big')
        try:
            header = json.loads(value[4 : 4 + n])
        except ValueError:
            return f'bytes value under {key!r} has a corrupt header'
        if header.get('key') != tag:
            return f'bytes value under {key!r} belongs to {header.get("key")!r}'
        if zlib.crc32(value[4 + n :]) != header.get('crc'):
            return f'bytes value under {key!r} ({len(value)} bytes) fails its CRC'
        expire_at = header.get('exp')
        recorded = header.get('tag', 'None')
    else:
        return f'unexpected value type under {key!r}: {type(value).__name__}'
    if stored_tag is not UNCHECKED and recorded != repr(stored_tag):
        return f'{key!r} read back the tag {stored_tag!r} with a value stored with {recorded}'
    if stored_expire is not UNCHECKED:
        if (stored_expire is None) != (expire_at is None):
            return f'{key!r} was written with expire time {expire_at!r} and the cache stored {stored_expire!r}'
        if expire_at is not None:
            if stored_expire < expire_at - STORED_EXPIRY_SLACK:
                return (
                    f'{key!r} has a stored expiry {expire_at - stored_expire:.3f}s before the one its writer recorded'
                )
            if now > stored_expire + STORED_EXPIRY_SLACK:
                return f'{key!r} returned {now - stored_expire:.3f}s after its stored expiry'
        return None
    if expire_at is not None and now > expire_at + EXPIRY_SLACK:
        return f'{key!r} returned {now - expire_at:.1f}s after it expired'
    return None


# ---------------------------------------------------------------------------
# The worker
# ---------------------------------------------------------------------------


class Worker:
    def __init__(
        self,
        run_dir: Path,
        worker_id: int,
        threads: int,
        seed: int,
        every: int = 0,
        thread_rate: float = 0.0,
    ) -> None:
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
        # How far the wall clock has moved against the monotonic one since this module loaded: a
        # step (NTP, or the OS around an update) makes expiry checks misfire, and this tells the two
        # apart from a real stale read.
        drift = (time.time() - time.monotonic()) - CLOCK_OFFSET_AT_START
        clock = f' [wall clock {drift:+.3f}s vs monotonic since start]' if abs(drift) >= 0.05 else ''
        with self.lock:
            self.problem_count += 1
            if len(self.problems) < MAX_PROBLEMS_KEPT:
                self.problems.append(f'{time.strftime("%H:%M:%S")} {text}{clock}')

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
        key = soak_key(rng, 'c', rng.randrange(CHAOS_KEYS))
        # Paced runs are slow enough to time every call, so the maximum is the true worst case.
        sample = self.thread_rate > 0 or i % LATENCY_SAMPLE_EVERY == 0
        if i % every == 0 and rng.random() < 0.25:
            asked_at = time.time()
            value, stored_expire, tag = self.chaos.pop(key, expire_time=True, tag=True)
            self.count('pops_chaos')
            if value is not None and (wrong := check_value(key, value, asked_at, tag, stored_expire)) is not None:
                self.problem(f'pop: {wrong}')
            self.ledger_op(rng)
        elif i % every == 0:
            expire = rng.choice((None, None, rng.uniform(1, 60)))
            expire_at = None if expire is None else time.time() + expire
            tag = soak_tag(rng)
            value = make_value(key, rng, expire_at, tag)
            start = time.perf_counter_ns()
            self.chaos.set(key, value, expire=expire, tag=tag)
            self.record('set', time.perf_counter_ns() - start)  # Writes are rare enough to time every one.
            if rng.random() < 0.01:
                self.chaos.evict(rng.choice(SOAK_TAGS))
                self.count('evicts_chaos')
            self.ledger_op(rng)
        else:
            asked_at = time.time()  # Expired means expired before we asked, not by the time we look.
            with_tag = rng.random() < 0.1
            start = time.perf_counter_ns()
            if with_tag:
                value, stored_expire, tag = self.chaos.get(key, expire_time=True, tag=True)
            else:
                (value, stored_expire), tag = self.chaos.get(key, expire_time=True), UNCHECKED
            if sample and not with_tag:
                self.record('get', time.perf_counter_ns() - start)
            if value is None:
                with self.lock:
                    self.misses += 1
            elif (wrong := check_value(key, value, asked_at, tag, stored_expire)) is not None:
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
                with self.ledger.transact():
                    tx = int(self.ledger.incr(f'hctr:{self.id}'))
                    for key, version in planned.items():
                        if version:
                            assert self.ledger.set(key, held_value(key, version, tx, rng)) is True
                        elif rng.random() < 0.5:
                            popped = self.ledger.pop(key)
                            pops += 1  # Counted after the transaction: see the note above.
                            if (got := held_version(key, popped)) != self.held.get(key, 0):
                                found.append(
                                    f'{key} popped {got} inside a transaction, last committed {self.held.get(key, 0)}'
                                )
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
                self.problem(
                    f'held transaction {outcome}, but its keys read {seen}: expected {expected} (before {before})'
                )
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
                self.queue_round(f'q.{token}', tick)
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
            self.problem(
                f'duplicate delivery: {slot} gave {token} to worker {self.id}, which another pop had already delivered'
            )

    def queue_round(self, token: str, tick: int) -> None:
        """The queue calls on the same ledger (plan 24, release 4): push this thread's token onto one
        queue every worker shares, record it, then pull one item from the front and deliver it as the
        slot lane does, so a token pulled twice, or pushed under a key some other push returned, shows up
        as a duplicate delivery or a lost token at the end."""
        self.ledger.push(token, prefix=QUEUE_PREFIX)
        self.ledger.set(f'pqw:{token}', tick)
        self.count('queue_pushed')
        key, pulled = self.ledger.pull(prefix=QUEUE_PREFIX)
        if key is None:
            return
        if not isinstance(pulled, str) or not pulled.startswith('q.'):
            self.problem(f'{key} pulled {pulled!r}, which no producer pushed')
            return
        self.count('queue_pulled')
        while True:
            try:
                first = self.ledger.add(f'delivered:{pulled}', self.id)
                break
            except Timeout:
                with self.lock:
                    self.timeouts += 1
        if not first:
            self.count('pq_duplicates')
            self.problem(f'duplicate delivery: {key} pulled {pulled}, which another pull had already delivered')

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
                'maint_bg': self.background_maintenance(),
                'fork_max_s': self.fork_max_s,
                'child_max_s': self.child_max_s,
                **self.extra_stats(),
            }
        return snap

    def extra_stats(self) -> dict[str, Any]:
        return {}

    def background_maintenance(self) -> dict[str, Any]:
        """Background maintenance's counts on every cache this worker holds (plan 28), summed: the
        chaos and ledger caches, and a role's own (the durable, behind and stuck caches on the lanes,
        where a long-held write lock makes a starved streak likeliest). A `maintenance_stats()` that
        raises is counted in `errors`, which the orchestrator reports."""
        total: dict[str, Any] = {'turns': 0, 'removed': 0, 'failures': 0, 'errors': 0, 'last_error': None}
        seen: set[int] = set()
        for cache in list(vars(self).values()):
            if not hasattr(cache, 'maintenance_stats') or id(cache) in seen:
                continue
            seen.add(id(cache))
            try:
                stats = cache.maintenance_stats()
            except Exception:
                total['errors'] += 1
                continue
            for key in ('turns', 'removed', 'failures'):
                total[key] += stats[key]
            total['last_error'] = total['last_error'] or stats['last_error']
        return total

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
        bucket = (
            'lost_deletes'
            if 'lost invalidations' in text
            else 'busy'
            if ' busy' in text
            else 'broken'
            if ' failing' in text
            else ''
        )
        if not bucket:
            return  # A decode report, or anything else: not what this checks.
        now = time.monotonic()
        where = (
            str(record.args[0]) if isinstance(record.args, tuple) and record.args else '',
            bucket,
        )
        last = self.last.get(where)
        self.last[where] = now
        self.lines[bucket] += 1
        # A broken line always goes to the log: its error text is what decides if it's a real finding.
        if bucket == 'broken' or sum(self.lines.values()) <= self.KEEP:
            print(record.getMessage(), file=sys.stderr, flush=True)
        if last is not None and now - last < LOG_MIN_GAP and not self.closing:
            self.worker.problem(
                f'durable log: two {bucket} lines for one store {now - last:.1f}s apart, under the 60 s limit'
            )


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
        self.inflight: dict[int, tuple[str, float]] = {}  # thread -> the timed call it's in, and when it began
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
        def spawn(target, *args):
            return threading.Thread(target=target, args=args, daemon=True)

        return [
            spawn(self.slow_call_main),
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
        me = threading.get_ident()
        self.inflight[me] = (what, start)
        try:
            return work()
        finally:
            self.inflight.pop(me, None)
            over = time.monotonic() - start - budget
            with self.lock:
                self.max_overrun = max(self.max_overrun, over)
            if over > OVERRUN_ALLOWANCE:
                self.count('budget_overruns')
                self.problem(
                    f'{what} took {over + budget:.2f}s against a budget of {budget}s (allowance {OVERRUN_ALLOWANCE}s)'
                )

    def slow_call_main(self) -> None:
        """Samples this process while a timed call is slow (SLOW_SAMPLE_AFTER)."""
        last = -SLOW_SAMPLE_GAP
        while not self.stop.is_set():
            time.sleep(0.1)
            now = time.monotonic()
            if now - last < SLOW_SAMPLE_GAP:
                continue
            slow = [
                (what, round(now - start, 2))
                for what, start in list(self.inflight.values())
                if now - start > SLOW_SAMPLE_AFTER
            ]
            if not slow:
                continue
            last = now
            self.count('slow_samples')
            out = self.run_dir / 'stalls' / f'slow-{self.id}-{time.strftime("%H%M%S")}'
            try:
                with open(f'{out}-python.txt', 'w') as f:
                    f.write(f'slow calls in flight: {slow}\n\n')
                    f.flush()
                    faulthandler.dump_traceback(f, all_threads=True)
                if sys.platform == 'darwin':
                    subprocess.Popen(
                        ['sample', str(os.getpid()), '1', '-file', f'{out}-native.txt'],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
            except OSError:
                pass

    def child_work(self) -> None:
        rng = random.Random()
        key = 'b:fork-child'
        try:
            self.behind.set(key, make_value(key, rng, None))
            if (value := self.behind.get(key)) is not None and (
                wrong := check_value(key, value, time.time())
            ) is not None:
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
                    self.timed(
                        'behind set',
                        BEHIND_TIMEOUT,
                        lambda: self.behind.set(key, payload),
                    )
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
                    with self.stuck_held.transact():
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
            self.problem(
                f'{call} behind a stuck fork had not returned after {STUCK_CALLER_LIMIT}s (timeout {STUCK_TIMEOUT}s): '
                f'the wedge'
            )
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
                with self.hog.transact():
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
        acked: dict[
            str, int
        ] = {}  # key -> the last version acknowledged (0: not there). Only this thread writes these keys.
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
                self.problem(
                    f'durable {after} returned a failure for {key}, but it reads version {got}, not the previous {want}'
                )

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
                if rng.random() < 0.1 and (read := self.truth_version(key)) not in (
                    version,
                    None,
                ):
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
        for what, work in (
            ('a key of no key type to set', lambda: self.durable.set(cast(Any, [123]), 'v')),
            ('a key of no key type to get', lambda: self.durable.get(cast(Any, [123]))),
        ):
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
            self.problem(
                f'final check: {answered} durable set calls returned False, but the handle counted {counted} failures'
            )

    def extra_stats(self) -> dict[str, Any]:
        lines = dict(self.log.lines)
        return {
            'durable': self.durable._durable_stats(),
            'log_lines': lines,
            'max_overrun': self.max_overrun,
            'stuck_caller_max_s': self.stuck_caller_max_s,
        }

    def close_all(self) -> None:
        self.log.closing = True
        for cache in (
            self.durable,
            self.truth,
            self.hog,
            self.behind,
            self.stuck_held,
            self.stuck_call,
        ):
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

    def __init__(
        self,
        run_dir: Path,
        worker_id: int,
        seed: int,
        role: str,
        store: int,
        stores: int,
    ) -> None:
        super().__init__(run_dir, worker_id, 0, seed)
        self.role = role
        self.store = store
        self.phase = AUTO_PERIOD * store / max(1, stores)  # Staggered, so stores burst in turn.
        self.auto = Cache(
            run_dir / f'auto-{store}',
            timeout=30.0,
            size_limit=2**34,
            wal_size_limit=AUTO_WAL_LIMIT,
            wal_checkpoint='auto',
            # This group measures the forced checkpoints writes make; background maintenance's own
            # checkpoints (plan 28) would take that work from them. Store 0 keeps it on, so the soak
            # still sees the two kinds meet (a background one must never stop a forced one).
            maintenance_interval=None if store == 0 else 0,
        )
        self.wal_path = run_dir / f'auto-{store}' / 'diskcache-oxide.sqlite-wal'
        self.peak_wal = 0

    def bursting(self) -> bool:
        return (time.time() + self.phase) % AUTO_PERIOD < AUTO_BURST

    def lane_threads(self) -> list[threading.Thread]:
        if self.role == 'auto-reader':
            return [
                threading.Thread(target=self.reader_main, args=(n,), daemon=True) for n in range(AUTO_READER_THREADS)
            ]
        return [
            threading.Thread(target=self.writer_main, daemon=True),
            threading.Thread(target=self.forks_main, daemon=True),
        ]

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
        return {
            'store': self.store,
            'auto': {
                **wal_stats(self.auto),
                'peak_wal': self.peak_wal,
                'limit': AUTO_WAL_LIMIT,
            },
        }

    def close_all(self) -> None:
        self.auto.close()
        super().close_all()


def open_fanout(run_dir: Path) -> FanoutCache:
    return FanoutCache(run_dir / 'fanout', timeout=30.0, shards=FANOUT_SHARDS, size_limit=FANOUT_SIZE_LIMIT)


def db_fds_inside(root: Path) -> list[str] | None:
    """The paths of this process's fds on a database, WAL or -shm file under `root`, one per fd, or
    None where that can't be read. Value files are left out: a read of a spilled value holds one for
    a moment."""
    import fcntl

    prefix = os.path.realpath(root) + os.sep
    fd_dir = '/dev/fd' if sys.platform == 'darwin' else '/proc/self/fd'
    found: list[str] = []
    try:
        names = os.listdir(fd_dir)
    except OSError:
        return None
    for name in names:
        try:
            if sys.platform == 'darwin':
                path = fcntl.fcntl(int(name), fcntl.F_GETPATH, bytes(1024)).rstrip(b'\0').decode()
            else:
                path = os.readlink(f'{fd_dir}/{name}')
        except OSError:
            continue  # Closed since the listing, or not a file.
        if path.startswith(prefix) and os.path.basename(path).startswith('diskcache-oxide.sqlite'):
            found.append(path[len(prefix) :])
    return found


class FanoutWorker(Worker):
    """A FanoutCache on fanout/ (plan 13, step 13g): its routed calls, merges and fanned-out
    maintenance from several threads, forks while they run, and its fd budget."""

    def __init__(self, run_dir: Path, worker_id: int, threads: int, seed: int) -> None:
        super().__init__(run_dir, worker_id, 0, seed)
        self.role = 'fanout'
        self.fan_threads = threads
        self.fan = open_fanout(run_dir)
        self.fan_root = run_dir / 'fanout'
        # Protected by self.lock: `fctr:<id>` increments that returned (only this worker writes it).
        self.fan_acked = 0
        # `fown:<id>:<n>` -> the version last acknowledged, under its own lock: a write and its
        # read-back are held across it (up to `timeout` each on a busy shard), which must not stall
        # every thread's counters, heartbeats and stats behind self.lock.
        self.fan_owned_lock = threading.Lock()
        self.fan_owned: dict[str, int] = {}
        self.fan_max_sampled_fds = 0  # The most seen at a check, not a continuous peak.
        # One key per shard, for the forked child to touch every store. Its own, since another fanout
        # worker's child may run at the same time (one child at a time per worker).
        self.child_prefix = f'fchild:{worker_id}:'
        self.per_shard: dict[int, str] = {}
        i = 0
        while len(self.per_shard) < self.fan.shards:
            key = f'{self.child_prefix}{i}'
            self.per_shard.setdefault(self.fan.shard_of(key), key)
            i += 1

    def lane_threads(self) -> list[threading.Thread]:
        load = [threading.Thread(target=self.fan_main, args=(n,), daemon=True) for n in range(self.fan_threads)]
        return [
            *load,
            threading.Thread(target=self.walk_main, daemon=True),
            threading.Thread(target=self.forks_main, daemon=True),
        ]

    # -- routed calls ---------------------------------------------------------------------------

    def fan_main(self, n: int) -> None:
        rng = random.Random(self.seed * 1_000 + n)
        next_at = time.monotonic()
        i = 0
        while not self.stop.is_set():
            next_at, stopping = pace(1.0 / FANOUT_CALLS_PER_S, next_at, self.stop)
            if stopping:
                break
            i += 1
            if i % 256 == 1:
                self.beat(f'fanout-{n}')
            try:
                self.fan_op(rng)
                self.count('fanout_ops')
                with self.lock:
                    self.ops += 1
            except Timeout:
                with self.lock:
                    self.timeouts += 1
            except Exception:
                self.problem(f'fanout thread {n}: {traceback.format_exc(limit=6)}')
                time.sleep(0.1)

    def fan_op(self, rng: random.Random) -> None:
        roll = rng.random()
        key = soak_key(rng, 'f', rng.randrange(FANOUT_KEYS))
        if roll < 0.70:
            asked_at = time.time()
            value, stored_expire = self.fan.get(key, expire_time=True)
            if value is None:
                with self.lock:
                    self.misses += 1
            elif (wrong := check_value(key, value, asked_at, stored_expire=stored_expire)) is not None:
                self.problem(f'fanout get: {wrong}')
        elif roll < 0.85:
            expire = rng.choice((None, None, rng.uniform(1, 60)))
            expire_at = None if expire is None else time.time() + expire
            self.fan.set(key, make_value(key, rng, expire_at), expire=expire)
        elif roll < 0.88:
            asked_at = time.time()
            value, stored_expire = self.fan.pop(key, expire_time=True)
            if (
                value is not None
                and (wrong := check_value(key, value, asked_at, stored_expire=stored_expire)) is not None
            ):
                self.problem(f'fanout pop: {wrong}')
        elif roll < 0.90:
            self.fan.delete(key)
        elif roll < 0.95:
            self.fan.incr(f'fctr:{self.id}')
            with self.lock:
                self.fan_acked += 1
        else:
            owned = f'fown:{self.id}:{rng.randrange(FANOUT_OWNED)}'
            # Two of this worker's threads may pick the same key; the lock keeps the version exact.
            with self.fan_owned_lock:
                version = self.fan_owned.get(owned, 0) + 1
                assert self.fan.set(owned, {'key': owned, 'v': version}) is True
                self.fan_owned[owned] = version
                got = self.fan.get(owned)
            if not isinstance(got, dict) or got.get('v') != version:
                self.problem(f'{owned} read back {got!r} right after writing version {version}')

    # -- merges, iteration, maintenance and the fd budget ---------------------------------------

    def walk_main(self) -> None:
        next_maint = time.monotonic() + FANOUT_MAINT_EVERY
        rounds = 0
        while not self.stop.wait(FANOUT_WALK_EVERY):
            self.beat('fanout-walk')
            rounds += 1
            try:
                self.walk(rounds)
                if time.monotonic() >= next_maint:
                    next_maint = time.monotonic() + FANOUT_MAINT_EVERY
                    self.maintain(rounds)
                if rounds % FANOUT_REOPEN_EVERY == 0:
                    self.reopen_fan()
                self.check_fds()
            except Timeout:
                with self.lock:
                    self.timeouts += 1
            except Exception:
                self.problem(f'fanout walk: {traceback.format_exc(limit=6)}')

    def walk(self, rounds: int) -> None:
        """A merge must be strictly sorted (nothing twice), `__iter__` must repeat nothing, and every
        owned key acknowledged before the walk must be in it (owned keys are never removed)."""
        with self.fan_owned_lock:
            owned = set(self.fan_owned)
        mine = list(self.fan.iterkeys(prefix=f'fown:{self.id}:'))
        if any(a >= b for a, b in zip(mine, mine[1:], strict=False)):
            self.problem('fanout iterkeys(prefix=) is out of order or repeats a key')
        if missing := owned - set(mine):
            self.problem(f'fanout iterkeys(prefix=) lost {len(missing)} owned keys, e.g. {sorted(missing)[:3]}')
        backwards = list(self.fan.iterkeys(prefix=f'fown:{self.id}:', reverse=True))
        if any(a <= b for a, b in zip(backwards, backwards[1:], strict=False)):
            self.problem('fanout iterkeys(reverse=True) is out of order or repeats a key')
        self.count('fanout_merges')
        if rounds % 4 == 0:  # Every key, both ways: the whole root is larger, so less often.
            merged = list(self.fan.iterkeys())
            # D-046: every str key first, strictly sorted, then the typed keys; nothing twice.
            texts = [k for k in merged if isinstance(k, str)]
            if merged[: len(texts)] != texts or any(a >= b for a, b in zip(texts, texts[1:], strict=False)):
                self.problem('fanout iterkeys() over every key is out of order or repeats a key')
            if len(set(map(repr, merged))) != len(merged):
                self.problem('fanout iterkeys() over every key repeats a key')
            walked = list(self.fan)
            if len(walked) != len(set(walked)):
                self.problem(f'fanout __iter__ repeated {len(walked) - len(set(walked))} keys')
            if missing := owned - set(walked):
                self.problem(f'fanout __iter__ lost {len(missing)} owned keys, e.g. {sorted(missing)[:3]}')
            self.count('fanout_full_walks')

    def maintain(self, rounds: int) -> None:
        """Fanned-out maintenance under one timeout: a partial failure must say how far it got."""
        ops = ('expire', 'cull', 'checkpoint')
        op = ops[rounds % len(ops)]
        try:
            getattr(self.fan, op)(timeout=FANOUT_MAINT_TIMEOUT)
            self.count('fanout_maint')
        except Timeout as ex:
            failed = getattr(ex, 'failed_shards', None)
            removed = getattr(ex, 'removed', 0 if op != 'checkpoint' else None)
            if not isinstance(failed, tuple) or not failed or (op != 'checkpoint' and not isinstance(removed, int)):
                self.problem(f'fanout {op} timed out without failed_shards/removed: {ex!r}')
            self.count('fanout_maint_partial')

    def reopen_fan(self) -> None:
        old = self.fan
        self.fan = open_fanout(self.run_dir)
        old.close()
        with self.lock:
            self.reopens += 1

    def check_fds(self) -> None:
        paths = db_fds_inside(self.fan_root)
        if paths is None:
            return
        with self.lock:
            self.fan_max_sampled_fds = max(self.fan_max_sampled_fds, len(paths))
        if len(paths) > FANOUT_FD_BUDGET:
            by_path = collections.Counter(paths)
            self.problem(
                f'{len(paths)} fds on fanout databases, over the budget of {FANOUT_FD_BUDGET} (D-037): '
                f'{dict(sorted(by_path.items()))}'
            )
        self.count('fanout_fd_checks')

    # -- forks ----------------------------------------------------------------------------------

    def forks_main(self) -> None:
        # Forks while the routed calls and the walks run, on top of the orchestrator's.
        while not self.stop.wait(FANOUT_FORK_EVERY):
            self.beat('fanout-forks')
            self.fork_test()
            self.count('fanout_forks')

    def child_work(self) -> None:
        """Every shard, through the inherited handle: a write, its read-back, and a merge."""
        tag = os.getpid()
        for key in self.per_shard.values():
            value = {'key': key, 'crc': zlib.crc32(b''), 'body': '', 'exp': None, 'pid': tag}
            assert self.fan.set(key, value) is True
            got = self.fan.get(key)
            assert isinstance(got, dict) and got.get('pid') == tag, f'{key} read back {got!r} in the child'
        merged = list(self.fan.iterkeys(prefix=self.child_prefix))
        assert merged == sorted(self.per_shard.values()), f'the child merged {merged!r}'

    # -- the end --------------------------------------------------------------------------------

    def verify_owned(self) -> None:
        super().verify_owned()
        with self.fan_owned_lock:
            owned = sorted(self.fan_owned.items())
        for key, version in owned:
            got = self.fan.get(key)
            if not isinstance(got, dict) or got.get('v') != version:
                self.problem(f'final check: {key} is {got!r}, last acknowledged version {version}')
        if (count := self.fan.get(f'fctr:{self.id}', 0)) != self.fan_acked:
            self.problem(f'final check: fctr:{self.id} is {count}, acknowledged increments {self.fan_acked}')

    def extra_stats(self) -> dict[str, Any]:
        return {
            'fanout': {
                'shards': FANOUT_SHARDS,
                'acked': self.fan_acked,
                'owned': len(self.fan_owned),  # A dict's len is safe to read without its lock.
                'max_sampled_fds': self.fan_max_sampled_fds,
                'fd_budget': FANOUT_FD_BUDGET,
            },
        }

    def close_all(self) -> None:
        self.fan.close()
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
            problems.append(
                f'worker {wid}: hctr is {count}, but it has {len(txs)} manifests (highest {max(txs, default=0)})'
            )
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
        problems.extend(
            f'{key} is {found}, but no committed transaction wrote it'
            for key, found in sorted(got.items())
            if key not in last
        )
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
    queued |= {ledger.get(key) for key in ledger.iterkeys(prefix=f'{QUEUE_PREFIX}-')}
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
        # Tokens a kill can lose per worker: each thread finishes one delivery before it starts the next,
        # so it has at most one token removed and not yet recorded.
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

    def pick(q):
        return ordered[min(len(ordered) - 1, int(q * len(ordered)))] / 1_000

    return {
        'n': len(ordered),
        'p50_us': pick(0.50),
        'p99_us': pick(0.99),
        'max_us': ordered[-1] / 1_000,
    }


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
    if op == 'cull':
        # The ledger has nothing expired, and under 'none' a cull evicts nothing for size.
        with open_ledger(run_dir) as ledger:
            culled = ledger.cull()
        if culled:
            print(f"ledger cull under eviction_policy='none' removed {culled} entries")
            return 1
    return 0


def main() -> int:
    from tests.proctitle import set_title

    # A short background maintenance interval for every cache this worker opens without its own, set
    # by soak --maintenance-interval (plan 28); the soak's tooling, not the library, reads it.
    interval = float(os.environ.get('DCO_MAINTENANCE_INTERVAL', '0') or 0)
    if interval:
        _core._set_default_maintenance_interval(interval)

    mode, run_dir = sys.argv[1], Path(sys.argv[2])
    if mode == 'maint':
        set_title(f'soak-maint-{sys.argv[3]}')
        return maint(run_dir, sys.argv[3])
    if mode == 'lanes':
        worker_id, seed, slot = int(sys.argv[3]), int(sys.argv[4]), int(sys.argv[5])
        set_title(f'soak-lanes-{worker_id}')
        return LaneWorker(run_dir, worker_id, seed, slot).run()
    if mode == 'fanout':
        worker_id, seed, threads = (int(x) for x in sys.argv[3:6])
        set_title(f'soak-fanout-{worker_id}')
        return FanoutWorker(run_dir, worker_id, threads, seed).run()
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
