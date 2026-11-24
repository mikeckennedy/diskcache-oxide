"""Reads only, across worker processes: where does a plain Cache's multi-process fall come from?

Usage (from the repo root, on AC power, nothing else heavy running, not under nice):
    venv/bin/python -m bench.read_procs
    venv/bin/python -m bench.read_procs --quick
    venv/bin/python -m bench.read_procs --counts 1,2,3,4 --only 'oxide Cache' --only 'raw sqlite3'

Each spawned process opens the cache and calls `get` in a share-nothing loop over 1,000 keys of a
1 KB str, with no writer anywhere. Plan 31's step 6 review found that a plain Cache falls past two
processes even here, so the fall is on the read side; plan 33 added the controls that say why:

- a directory per process: nothing shared, so the ceiling, and the yardstick for the other rows;
- diskcache's plain Cache and a raw `sqlite3` loop on one WAL file, for whether the fall is ours;
- threads in one process on one shared handle (free-threaded builds only), where SQLite counts
  readers per process and skips the lock system calls when they overlap;
- a 6-byte value (smaller, and the whole database fits SQLite's page cache) and a larger
  `sqlite_cache_size` on the 1 KB value, to tell `pread`s from locking;
- background maintenance off, since each process otherwise has a second thread;
- a WAL left with frames in it, where the others start from a checkpointed one. A database opened
  with an empty WAL has no page count in the WAL index, so SQLite calls `fstat` on the database file
  at the start of every read until the first commit, and that call is contended across processes
  too;
- the raw `sqlite3` loop again with a second connection in each process holding a read transaction
  open (a sentinel), which keeps the process's lock count above zero so its reads make no lock
  system call: what removing the lock traffic would give back.

Rounds alternate the variants; the median round is reported, with the share of the workers' CPU
time spent in the kernel beside it. Writes bench/results/read-procs-<python>-<machine>.{json,md}.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import multiprocessing
import os
import platform
import queue
import random
import resource
import shutil
import sqlite3
import statistics
import sys
import sysconfig
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Optional

RESULTS_DIR = Path(__file__).parent / 'results'
DEFAULT_COUNTS = (1, 2, 3, 4, 8, 16)
KEYS = 1_000
SELECT = 'SELECT value FROM t WHERE key = ?'


@dataclasses.dataclass(frozen=True)
class Variant:
    name: str
    kind: str  # 'oxide', 'oxide-fanout', 'diskcache', 'diskcache-fanout' or 'sqlite3'
    value_len: int = 1_024
    options: tuple[tuple[str, Any], ...] = ()
    per_worker: bool = False  # a directory per worker: nothing shared
    threads: bool = False  # one process, the workers are threads on one shared handle
    frames: bool = False  # the parent holds the cache open with frames in its WAL
    sentinel: bool = False  # each worker also holds a read transaction open on a second connection
    env: tuple[tuple[str, str], ...] = ()  # set in each worker; these rows run only with --prototype


# Plan 33's candidate S, a sentinel connection per store: bench/results/read-sentinel-prototype-2026-11-24.diff.
PROTOTYPE_S = (('DCO_PROTO_SENTINEL', '1'),)
# The same, taken only after this many operations in a row saw no change to the database.
QUIET_64 = (*PROTOTYPE_S, ('DCO_PROTO_SENTINEL_QUIET', '64'))
QUIET_512 = (*PROTOTYPE_S, ('DCO_PROTO_SENTINEL_QUIET', '512'))
SHARED = 'oxide Cache'
CEILING = 'oxide Cache, directory per process'
VARIANTS = (
    Variant(SHARED, 'oxide'),
    Variant(CEILING, 'oxide', per_worker=True),
    Variant('oxide FanoutCache(8)', 'oxide-fanout'),
    Variant('diskcache Cache', 'diskcache'),
    Variant('diskcache FanoutCache(8)', 'diskcache-fanout'),
    Variant('raw sqlite3, one WAL file', 'sqlite3'),
    Variant('raw sqlite3, with a sentinel reader', 'sqlite3', sentinel=True),
    Variant('raw sqlite3, WAL with frames', 'sqlite3', frames=True),
    Variant('raw sqlite3, sentinel reader, WAL with frames', 'sqlite3', sentinel=True, frames=True),
    Variant('oxide Cache, threads in one process', 'oxide', threads=True),
    Variant('oxide Cache, 6 B value (fits the page cache)', 'oxide', value_len=6),
    Variant('oxide Cache, sqlite_cache_size=8192', 'oxide', options=(('sqlite_cache_size', 8_192),)),
    Variant('oxide Cache, maintenance off', 'oxide', options=(('maintenance_interval', 0),)),
    Variant('oxide Cache, WAL with frames', 'oxide', frames=True),
    Variant('oxide Cache, threads, WAL with frames', 'oxide', threads=True, frames=True),
    Variant('oxide FanoutCache(8), WAL with frames', 'oxide-fanout', frames=True),
    Variant('prototype S: oxide Cache', 'oxide', env=PROTOTYPE_S),
    Variant(
        'prototype S: oxide Cache, sqlite_cache_size=8192',
        'oxide',
        options=(('sqlite_cache_size', 8_192),),
        env=PROTOTYPE_S,
    ),
    Variant('prototype S: oxide Cache, threads in one process', 'oxide', threads=True, env=PROTOTYPE_S),
    Variant('prototype S: oxide FanoutCache(8)', 'oxide-fanout', env=PROTOTYPE_S),
    Variant('prototype S, quiet 64: oxide Cache', 'oxide', env=QUIET_64),
    Variant('prototype S, quiet 512: oxide Cache', 'oxide', env=QUIET_512),
    Variant('prototype S, quiet 64: oxide Cache, threads in one process', 'oxide', threads=True, env=QUIET_64),
    Variant('prototype S, quiet 64: oxide FanoutCache(8)', 'oxide-fanout', env=QUIET_64),
    Variant('prototype S, quiet 512: oxide FanoutCache(8)', 'oxide-fanout', env=QUIET_512),
    Variant('prototype S: oxide Cache, WAL with frames', 'oxide', frames=True, env=PROTOTYPE_S),
    Variant(
        'prototype S: oxide Cache, sqlite_cache_size=8192, WAL with frames',
        'oxide',
        options=(('sqlite_cache_size', 8_192),),
        frames=True,
        env=PROTOTYPE_S,
    ),
    Variant('prototype S: oxide Cache, threads, WAL with frames', 'oxide', threads=True, frames=True, env=PROTOTYPE_S),
    Variant('prototype S: oxide FanoutCache(8), WAL with frames', 'oxide-fanout', frames=True, env=PROTOTYPE_S),
)


class RawSqlite:
    """One autocommit connection doing the lookup a cache `get` does, with the cache's two methods."""

    def __init__(self, directory: str) -> None:
        os.makedirs(directory, exist_ok=True)
        self.path = os.path.join(directory, 'cache.db')
        self.sentinel: Optional[sqlite3.Connection] = None
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.execute('PRAGMA synchronous=NORMAL')
        self.connection.execute('CREATE TABLE IF NOT EXISTS t (key TEXT PRIMARY KEY, value TEXT)')

    def hold_reader(self) -> None:
        """A second connection that stays inside a read transaction. SQLite counts shared locks per
        process and calls `fcntl` only when a count leaves or returns to zero, so while this one holds
        the read mark the other connection's reads make no lock system call."""
        self.sentinel = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.sentinel.execute('BEGIN')
        self.sentinel.execute('SELECT 1 FROM t LIMIT 1').fetchall()

    def set(self, key: str, value: str) -> None:
        self.connection.execute('INSERT OR REPLACE INTO t VALUES (?, ?)', (key, value))

    def get(self, key: str) -> Optional[str]:
        row = self.connection.execute(SELECT, (key,)).fetchone()
        return row[0] if row else None

    def checkpoint(self) -> None:
        self.connection.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchall()

    def close(self) -> None:
        if self.sentinel is not None:
            self.sentinel.close()
        self.connection.close()


def open_cache(variant: Variant, directory: str, **extra: Any) -> Any:
    options = dict(variant.options) | extra
    if variant.kind == 'oxide':
        import diskcache_oxide

        return diskcache_oxide.Cache(directory, **options)
    if variant.kind == 'oxide-fanout':
        import diskcache_oxide

        return diskcache_oxide.FanoutCache(directory, shards=8, **options)
    if variant.kind == 'sqlite3':
        return RawSqlite(directory)
    import diskcache

    return (
        diskcache.FanoutCache(directory, shards=8) if variant.kind == 'diskcache-fanout' else diskcache.Cache(directory)
    )


def read_loop(cache: Any, seed: int, stop: Any, keys: list[str], writes: int, value: str) -> int:
    """Gets until `stop`, with `writes` sets spread evenly through every 1,000 calls. Returns the calls made."""
    rng = random.Random(seed)
    plan = [keys[rng.randrange(KEYS)] for _ in range(1_000)]
    get, put = cache.get, cache.set
    chunks = [plan[n * 1_000 // writes : (n + 1) * 1_000 // writes] for n in range(writes)] if writes else [plan]
    ops = 0
    while not stop.is_set():
        for chunk in chunks:
            for key in chunk:
                get(key)
            if writes:
                put(chunk[0], value)
        ops += len(plan)
    return ops


def cpu_times() -> tuple[float, float]:
    usage = resource.getrusage(resource.RUSAGE_SELF)
    return usage.ru_utime, usage.ru_stime


def worker(
    variant: Variant, directory: str, seed: int, count: int, writes: int, ready: Any, go: Any, stop: Any, results: Any
) -> None:
    """One process: `count` threads on one shared handle (1 unless the variant is the threaded one)."""
    os.environ.update(dict(variant.env))  # before the first cache call, which is when the build reads it
    cache = open_cache(variant, directory)
    if variant.sentinel:
        cache.hold_reader()
    keys = [''.join(('t:', str(i))) for i in range(KEYS)]
    value = 'v' * variant.value_len
    get = cache.get
    for key in keys:  # open, and warm the page cache, before the clock starts
        get(key)
    ops = [0] * count
    barrier = threading.Barrier(count + 1)

    def run(n: int) -> None:
        barrier.wait()
        ops[n] = read_loop(cache, seed * 1_000 + n, stop, keys, writes, value)

    pool = [threading.Thread(target=run, args=(n,)) for n in range(count)] if count > 1 else []
    for thread in pool:
        thread.start()
    ready.release()
    go.wait()
    user, system = cpu_times()
    if pool:
        barrier.wait()
        for thread in pool:
            thread.join()
    else:
        ops[0] = read_loop(cache, seed, stop, keys, writes, value)
    user_after, system_after = cpu_times()
    results.put((sum(ops), user_after - user, system_after - system))
    cache.close()


def measure(
    variant: Variant, directories: list[str], count: int, seconds: float, writes: int = 0
) -> tuple[float, float, int]:
    """`(calls a second, the share of the workers' CPU time spent in the kernel, WAL bytes at the end)`."""
    context = multiprocessing.get_context('spawn')
    ready, go, stop, results = context.Semaphore(0), context.Event(), context.Event(), context.Queue()
    if variant.threads:
        jobs = [(directories[0], 0, count)]
    else:
        jobs = [(directories[n % len(directories)], n, 1) for n in range(count)]
    pool = [
        context.Process(target=worker, args=(variant, directory, seed, threads, writes, ready, go, stop, results))
        for directory, seed, threads in jobs
    ]
    for process in pool:
        process.start()
    for _ in pool:
        if not ready.acquire(timeout=120):
            stop.set()
            go.set()
            raise RuntimeError(f'{variant.name}: a worker never got ready')
    go.set()
    start = time.perf_counter()
    time.sleep(seconds)
    wal = wal_bytes(directories[0])
    stop.set()
    try:
        got = [results.get(timeout=seconds + 120) for _ in pool]
    except queue.Empty:
        raise RuntimeError(f'{variant.name}: a worker never reported')
    elapsed = time.perf_counter() - start
    for process in pool:
        process.join()
    user, system = sum(g[1] for g in got), sum(g[2] for g in got)
    return sum(g[0] for g in got) / elapsed, system / (user + system) if user + system else 0.0, wal


def populate(variant: Variant, directory: str) -> None:
    cache = open_cache(variant, directory)
    for i in range(KEYS):
        cache.set(f't:{i}', 'v' * variant.value_len)
    if variant.kind in ('oxide', 'oxide-fanout', 'sqlite3'):
        cache.checkpoint()
    cache.close()


def wal_bytes(directory: str) -> int:
    return sum(path.stat().st_size for path in Path(directory).rglob('*-wal'))


def power_source() -> str:
    if sys.platform != 'darwin':
        return 'unknown'
    import subprocess

    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True, text=True, timeout=5).stdout
    except OSError, subprocess.SubprocessError:
        return 'unknown'
    return 'ac' if 'AC Power' in out else 'battery' if 'Battery Power' in out else 'unknown'


def table(title: str, variants: list[Variant], counts: list[int], cell: Any) -> list[str]:
    lines = [f'{title}\n', '| Variant | ' + ' | '.join(str(c) for c in counts) + ' |', '|---' * (len(counts) + 1) + '|']
    lines += [f'| {v.name} | ' + ' | '.join(cell(v.name, str(c)) for c in counts) + ' |' for v in variants]
    return lines + ['']


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--machine', default=platform.node().split('.')[0] or 'unknown')
    parser.add_argument('--quick', action='store_true', help='Smoke-test the script; the numbers mean nothing.')
    parser.add_argument('--rounds', type=int, help='Rounds per variant (default 5, quick 1).')
    parser.add_argument('--seconds', type=float, help='Seconds per point (default 1.5, quick 0.2).')
    parser.add_argument('--counts', help='Process counts, comma separated (default 1,2,3,4,8,16 up to the CPU count).')
    parser.add_argument(
        '--only',
        action='append',
        help='Run only the variant with this name, or those starting with it if none has it (repeatable).',
    )
    parser.add_argument(
        '--writes',
        type=int,
        default=0,
        help='Sets in every 1,000 calls of each worker (default 0; 1 is the 1,000 : 1 mix, 10 is 99 : 1).',
    )
    parser.add_argument('--prototype', action='store_true', help="Also the rows that need plan 33's prototype build.")
    parser.add_argument('--out', type=Path, default=RESULTS_DIR, help='Directory for the results.')
    args = parser.parse_args()
    rounds = args.rounds or (1 if args.quick else 5)
    seconds = args.seconds or (0.2 if args.quick else 1.5)
    cpus = os.cpu_count() or 1
    counts = [int(c) for c in args.counts.split(',')] if args.counts else [c for c in DEFAULT_COUNTS if c <= cpus]
    gil_disabled = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
    names = {v.name for v in VARIANTS}
    variants = [
        v
        for v in VARIANTS
        if (gil_disabled or not v.threads)
        and (args.prototype or not v.env)
        and not (args.writes and v.sentinel)  # a sentinel that never moves pins the WAL under writes
        and (not args.only or any(v.name == o or (o not in names and v.name.startswith(o)) for o in args.only))
    ]
    import diskcache_oxide

    meta = {
        'python': platform.python_version() + ('t' if gil_disabled else ''),
        'oxide': diskcache_oxide.__version__,
        'sqlite3_module': sqlite3.sqlite_version,
        'machine': args.machine,
        'platform': platform.platform(),
        'cpus': cpus,
        'power': power_source(),
        'nice': os.getpriority(os.PRIO_PROCESS, 0),
        'load_average': os.getloadavg()[0],
        'quick': args.quick,
        'rounds': rounds,
        'seconds_per_point': seconds,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    if not args.quick and (meta['power'] == 'battery' or meta['nice'] > 0):
        raise SystemExit(f'refusing a full run on {meta["power"]} power at nice {meta["nice"]}; use --quick')

    root = Path(tempfile.mkdtemp(prefix='dco-read-procs-'))
    samples: dict[str, dict[str, list[float]]] = {v.name: {str(c): [] for c in counts} for v in variants}
    kernel: dict[str, dict[str, list[float]]] = {v.name: {str(c): [] for c in counts} for v in variants}
    wal: dict[str, int] = {}
    wal_peak: dict[str, dict[str, int]] = {v.name: {str(c): 0 for c in counts} for v in variants}
    holders = []
    try:
        directories: dict[str, list[str]] = {}
        for n, variant in enumerate(variants):
            copies = max(counts) if variant.per_worker else 1
            directories[variant.name] = [str(root / f'{n}-{copy}') for copy in range(copies)]
            for directory in directories[variant.name]:
                populate(variant, directory)
            if variant.frames:
                # A handle that stays open keeps the WAL from being checkpointed away at the last close,
                # and with maintenance off in this process nothing here trims what it writes.
                holder = open_cache(variant, directories[variant.name][0], maintenance_interval=0)
                for i in range(KEYS):
                    holder.set(f't:{i}', 'v' * variant.value_len)
                holders.append(holder)
            wal[variant.name] = wal_bytes(directories[variant.name][0])
        for r in range(rounds):
            for variant in variants:
                for count in counts:
                    rate, share, wal_end = measure(variant, directories[variant.name], count, seconds, args.writes)
                    samples[variant.name][str(count)].append(rate)
                    kernel[variant.name][str(count)].append(share)
                    wal_peak[variant.name][str(count)] = max(wal_peak[variant.name][str(count)], wal_end)
                print(f'round {r + 1}/{rounds}: {variant.name} done', file=sys.stderr)
        wal_after = {v.name: wal_bytes(directories[v.name][0]) for v in variants}
    finally:
        for holder in holders:
            holder.close()
        shutil.rmtree(root, ignore_errors=True)

    results = {v: {c: statistics.median(s) for c, s in by_count.items()} for v, by_count in samples.items()}
    system = {v: {c: statistics.median(s) for c, s in by_count.items()} for v, by_count in kernel.items()}
    kind = f'{args.writes} sets in every 1,000 calls' if args.writes else 'Reads only'
    lines = [
        f'{kind}, {KEYS:,} keys of a 1 KB str unless the row says otherwise, one thread per process (the threaded '
        f'row: one process), median of {rounds} rounds of {seconds} s. oxide {meta["oxide"]}, Python {meta["python"]}, '
        f'{meta["machine"]} ({cpus} CPUs), {meta["power"]} power.\n'
    ]
    lines += table('Calls a second, by process count:', variants, counts, lambda v, c: f'{results[v][c] / 1_000:,.0f}k')
    if CEILING in results:
        lines += table(
            f'As a share of "{CEILING}" at the same count:',
            [v for v in variants if v.name != CEILING],
            counts,
            lambda v, c: f'{100 * results[v][c] / results[CEILING][c]:.0f}%',
        )
    lines += table(
        "Share of the workers' CPU time spent in the kernel:",
        variants,
        counts,
        lambda v, c: f'{100 * system[v][c]:.0f}%',
    )
    if args.writes:
        lines += table(
            'WAL size as each point ended, in KiB (the largest of the rounds; the first shard of a FanoutCache):',
            variants,
            counts,
            lambda v, c: f'{wal_peak[v][c] // 1_024:,}',
        )
    lines.append(
        'WAL bytes before and after the run: '
        + '; '.join(
            f'{v.name}: {wal[v.name]:,} and {wal_after[v.name]:,}'
            for v in variants
            if wal[v.name] or v.frames or args.writes
        )
        + '.\n'
    )
    text = '\n'.join(lines)
    mix = f'-{args.writes}-writes-per-1000' if args.writes else ''
    out = args.out / f'read-procs-{meta["python"]}-{meta["machine"]}{mix}{"-quick" if args.quick else ""}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {'meta': meta, 'samples': samples, 'results': results, 'kernel_share': system}
    out.write_text(
        json.dumps(payload | {'wal_bytes': {'before': wal, 'after': wal_after, 'at_point_end': wal_peak}}, indent=2)
        + '\n'
    )
    out.with_suffix('.md').write_text(text)
    print(text)
    print(f'Wrote {out}')


if __name__ == '__main__':
    main()
