"""FanoutCache benchmark (plan 13, step 13i; extended for plans 29 and 30): what sharding buys, and its memory cost.

Usage (from the repo root, on AC power, on a quiet machine):
    venv/bin/python -m bench.fanout                      # 3.14: the scaling group, 3 interleaved rounds
    .venv-ft/bin/python -m bench.fanout                  # 3.14t
    venv/bin/python -m bench.fanout --group all          # every group (see below for how long each takes)
    venv/bin/python -m bench.fanout --group pagecache --group sizing
    venv/bin/python -m bench.fanout --quick --group all  # smoke test, one short round
    venv/bin/python -m bench.fanout --only small --rounds 5
    venv/bin/python -m bench.fanout --mixes 99:1 --counts 1,2,4,8 --stress-counts none   # the plan-13 grid

Groups (--group, repeatable; default scaling). A variant in several groups runs each case once.
Times are for 3 rounds at the defaults on an M-series Mac; --group all takes about 30 minutes.
- scaling (about 15 minutes): does sharding scale, and what does it cost? oxide Cache at the default
  connection budget and at max_connections=16; oxide FanoutCache(8) at the default budget, at
  max_connections=16, and at sqlite_cache_size=32 (the default is 256 pages, 1 MiB, per connection,
  so a FanoutCache(8) thread holding all eight shards carries 8 MiB of page cache to a Cache's
  1 MiB; plan 30); stock diskcache Cache and FanoutCache(8). The 99:1 mix at 1, 4 and 8 (--full or
  --counts 1,2,4,8 adds 2), the 90:10 and 50:50 stress mixes at 8, threads and processes, uniform
  and Zipf keys, 1 KB values; plus the small cases.
- pagecache (profiling variants, not headline numbers; about 6 minutes, 3 more beside sizing): does
  a smaller page cache cost speed? oxide Cache at sqlite_cache_size 256 (the default), 128 and 64,
  beside FanoutCache(8) at the default, 64 and 32. 99:1 at 1 and 8, 90:10 at 8, threads and
  processes, uniform and Zipf, 1 KB values.
- sizing (page-cache sizing cases, about 15 minutes): is a small page cache stable, with values up
  to the inline limit? A value under 64 KiB is stored inline (spill::THRESHOLD), and one 64 KiB
  value's overflow chain is about 16 pages, so a 16- or 32-page cache per connection could thrash.
  oxide Cache at 256, 128 and 64 pages; FanoutCache(8) at the default, 64, 32 and 16; FanoutCache(32)
  at 64, 32 and 16. 1 KB, 16 KiB and 64 KiB values (64k is 65,472 characters, just under the
  threshold once encoded); 99:1 at 1 and 8 threads, 90:10 at 8, uniform and Zipf; threads only. A
  large-value database holds SIZING_BYTES (64 MiB), four times the largest variant's page-cache
  budget, so filling stays quick. Cut to fit the time: the 90:10 mix at 1 thread, the processes
  cases, and FanoutCache(32) at 1 KB (it runs 16 KiB and 64 KiB only).
pagecache and sizing print one row per case and variant: ops/s median, min-max and cv across rounds,
memory, and sampled get and set latency (p50, p99, p99.9), with UNSTABLE, TAIL-GET and TAIL-SET
flags on a row that stands out from the other variants in the same case (see print_detail). Every
case records the latency, whatever group it's in; about one get in 61 and one operation in 100
that's a write are timed with perf_counter_ns, into buffers allocated before the memory baseline.
Outside every group (pass --variant-list, which replaces each selected group's variants): oxide
FanoutCache(8) at 64 pages in the scaling grid, max_connections=32 on Cache and FanoutCache(8), to
tell a connection-budget limit from a shard one, and FanoutCache(8) with an explicit
sqlite_cache_size=256, which keeps today's default measurable once plan 30 splits the default.

Mixes (get:set): 99:1 is the headline. 90:10 and 50:50 are write-heavy stress cases, labelled as
such in the table; they show where sharding the write lock matters. Zipf (s=1.0) keys are the
hot-working-set case, where the page-cache size matters most.

Cases (N is the thread or process count; non-headline mixes and value sizes are in the name):
- threads-{uniform,zipf}[-90to10|-50to50][-v16k|-v64k]-N: N threads in one fresh process sharing one
  handle. 1 KB values over KEYS (default 50,000) keys: a ~50 MB database, larger than any variant's
  page-cache budget. ops/s for the process, and memory (below).
- procs-{uniform,zipf}[-90to10|-50to50]-N: the same mix from N spawned processes, one thread each,
  each opening the directory itself (a web garden). ops/s summed over the processes; memory per
  process, the median across them (not summed).
- small-1t / small-4t-shared / small-4t-separate: scale_probe's free-threaded small-read case (700
  URL-shaped keys, 6-byte values, reads only, 4 threads) on one shared handle, or on four separate
  instances of the variant (one directory each). This is what FanoutCache exists for. No memory.
--counts and --stress-counts replace every group's counts for the 99:1 mix and the others (`none`
drops them), --full runs 1, 2, 4 and 8 everywhere, --mixes, --dists and --sizes narrow the grid, and
--only PREFIX filters case names on top (threads, procs-zipf, threads-uniform-50to50, small...).

Memory. Every threads and procs case runs in fresh processes against a database filled beforehand
by another process, so filling never shows up in its numbers. A case process imports the variant's
module and builds its key plans, takes its RSS as the baseline, then opens the cache, warms up for
WARM_SECONDS, and runs the measured window. It reports, in MiB over that baseline: `rss` (current
RSS just after the window, before closing the cache) and `peak` (ru_maxrss at the same point, which
is bytes on macOS and KiB on Linux). Both include the threads' own stacks and Python objects, alike
for every variant; the differences between variants are the cache's. On Windows they're NaN.

Each round runs every variant in a fresh worker process (the order rotates each round, so drift hits
all of them alike), and each variant's databases are filled once per run, before its first round.
The driver reports the median over rounds and the min-max spread. Only compare results from the same
machine. Writes bench/results/fanout-<python>-<machine>.json: per variant and case, the ops/s
`median`/`min`/`max`/`samples` as before, plus `cv`, `mix`, `value_bytes`, `rss_growth_mib`,
`peak_growth_mib` and `latency_us` (get and set, each p50/p99/p999 with its own median, min, max,
cv and samples, and the sample counts `n`). Results from before this
extension used 1,000 keys, the 99:1 mix only and one process for all of a variant's cases;
`meta.keys` says which.
"""

from __future__ import annotations

import argparse
import itertools
import json
import math
import multiprocessing
import os
import platform
import random
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).parent / 'results'
THREAD_COUNTS = (1, 2, 4, 8)
KEYS = 50_000
QUICK_KEYS = 5_000
SIZING_BYTES = 64 * 2**20  # Values per sizing database: 4x the 16 MiB page cache of FanoutCache(8)'s default.
QUICK_SIZING_BYTES = 8 * 2**20
PLAN_LEN = 10_000
STOP_CHECK = 500
GET_SAMPLE_EVERY = 61  # Time about one get in 61 (prime, so it never lines up with the writes).
SAMPLES_PER_SECOND = 40_000  # Latency buffer per thread and kind; past it, samples stop (only at > 2.4M gets/s).
TIMEOUT = 10.0
SECONDS = 1.0
WARM_SECONDS = 0.2
MIB = 2**20

# Mix name -> one write in every N operations. The first is the headline; the others are stress cases.
MIXES: dict[str, int] = {'99:1': 100, '90:10': 10, '50:50': 2}
HEADLINE = '99:1'
DISTS = ('uniform', 'zipf')
# Value size label -> characters. 64k stays under spill::THRESHOLD (65,536 bytes) once encoded, so it's inline.
SIZES: dict[str, int] = {'1k': 1_024, '16k': 16_384, '64k': 65_472}
DEFAULT_SIZE = '1k'

SMALL_KEYS = 700
SMALL_OPS = 60_000  # Reads per thread, as in bench/scaling/scale_probe.py.

# variant -> (module, class, constructor kwargs). The shard count is part of the name.
VARIANTS: dict[str, tuple[str, str, dict[str, Any]]] = {
    'oxide-cache': ('diskcache_oxide', 'Cache', {}),
    'oxide-cache-c16': ('diskcache_oxide', 'Cache', {'max_connections': 16}),
    'oxide-cache-cs128': ('diskcache_oxide', 'Cache', {'sqlite_cache_size': 128}),
    'oxide-cache-cs64': ('diskcache_oxide', 'Cache', {'sqlite_cache_size': 64}),
    'oxide-fanout8': ('diskcache_oxide', 'FanoutCache', {'shards': 8}),
    'oxide-fanout8-c16': ('diskcache_oxide', 'FanoutCache', {'shards': 8, 'max_connections': 16}),
    'oxide-fanout8-cs64': ('diskcache_oxide', 'FanoutCache', {'shards': 8, 'sqlite_cache_size': 64}),
    'oxide-fanout8-cs32': ('diskcache_oxide', 'FanoutCache', {'shards': 8, 'sqlite_cache_size': 32}),
    'oxide-fanout8-cs16': ('diskcache_oxide', 'FanoutCache', {'shards': 8, 'sqlite_cache_size': 16}),
    'oxide-fanout32-cs64': ('diskcache_oxide', 'FanoutCache', {'shards': 32, 'sqlite_cache_size': 64}),
    'oxide-fanout32-cs32': ('diskcache_oxide', 'FanoutCache', {'shards': 32, 'sqlite_cache_size': 32}),
    'oxide-fanout32-cs16': ('diskcache_oxide', 'FanoutCache', {'shards': 32, 'sqlite_cache_size': 16}),
    'stock-cache': ('diskcache', 'Cache', {}),
    'stock-fanout8': ('diskcache', 'FanoutCache', {'shards': 8}),
    # In no group: wider budgets, to tell a connection-budget limit from a shard one, and an explicit 256 pages,
    # which stays today's default once plan 30 splits the default across the shards. Pass --variant-list.
    'oxide-cache-c32': ('diskcache_oxide', 'Cache', {'max_connections': 32}),
    'oxide-fanout8-c32': ('diskcache_oxide', 'FanoutCache', {'shards': 8, 'max_connections': 32}),
    'oxide-fanout8-cs256': ('diskcache_oxide', 'FanoutCache', {'shards': 8, 'sqlite_cache_size': 256}),
}


@dataclass(frozen=True)
class Group:
    about: str
    variants: tuple[str, ...]
    kinds: tuple[str, ...]
    mixes: tuple[str, ...]
    counts: tuple[int, ...]  # For the headline mix.
    stress_counts: tuple[int, ...]  # For the other mixes.
    sizes: tuple[str, ...] = (DEFAULT_SIZE,)
    small: bool = False
    detail: bool = False  # Print one row per case and variant, with spread, cv and latency percentiles.
    large_values_only: tuple[str, ...] = ()  # Variants that skip the 1 KB cases (to keep the group's run time).


PAGE_CACHE_VARIANTS = (
    'oxide-cache',
    'oxide-cache-cs128',
    'oxide-cache-cs64',
    'oxide-fanout8',
    'oxide-fanout8-cs64',
    'oxide-fanout8-cs32',
)
GROUPS: dict[str, Group] = {
    'scaling': Group(
        about='does sharding scale, and what does it cost',
        variants=(
            'oxide-cache',
            'oxide-cache-c16',
            'oxide-fanout8',
            'oxide-fanout8-c16',
            'oxide-fanout8-cs32',
            'stock-cache',
            'stock-fanout8',
        ),
        kinds=('threads', 'procs'),
        mixes=tuple(MIXES),
        counts=(1, 4, 8),
        stress_counts=(8,),
        small=True,
    ),
    'pagecache': Group(
        about='profiling variants: does a smaller page cache cost speed, threads and processes',
        variants=PAGE_CACHE_VARIANTS,
        kinds=('threads', 'procs'),
        mixes=('99:1', '90:10'),
        counts=(1, 8),
        stress_counts=(8,),
        detail=True,
    ),
    'sizing': Group(
        about='page-cache sizing cases: stability of small page caches, up to 64 KiB inline values',
        variants=(
            *PAGE_CACHE_VARIANTS,
            'oxide-fanout8-cs16',
            'oxide-fanout32-cs64',
            'oxide-fanout32-cs32',
            'oxide-fanout32-cs16',
        ),
        kinds=('threads',),
        mixes=('99:1', '90:10'),
        counts=(1, 8),
        stress_counts=(8,),
        sizes=tuple(SIZES),
        detail=True,
        large_values_only=('oxide-fanout32-cs64', 'oxide-fanout32-cs32', 'oxide-fanout32-cs16'),
    ),
}

# (kind, dist, mix, count, size label)
Case = tuple[str, str, str, int, str]
# (key, is_write, is_timed)
Plan = list[tuple[str, bool, bool]]


class Samples:
    """Preallocated (before the memory baseline) buffers of sampled get and set latencies, in ns."""

    def __init__(self, seconds: float) -> None:
        cap = int(seconds * SAMPLES_PER_SECOND) + 1_000
        self.gets = array('q', bytes(8 * cap))
        self.sets = array('q', bytes(8 * cap))
        self.n_gets = 0
        self.n_sets = 0

    def taken(self) -> tuple[list[int], list[int]]:
        return self.gets[: self.n_gets].tolist(), self.sets[: self.n_sets].tolist()


def percentiles(values: list[int]) -> dict[str, float]:
    """p50, p99 and p99.9 in microseconds, and the sample count."""
    if not values:
        return {'p50': math.nan, 'p99': math.nan, 'p999': math.nan, 'n': 0}
    values = sorted(values)

    def at(q: float) -> float:
        return values[min(len(values) - 1, int(q * len(values)))] / 1_000

    return {'p50': at(0.5), 'p99': at(0.99), 'p999': at(0.999), 'n': len(values)}


def latency_report(samples: list[Samples]) -> dict[str, dict[str, float]]:
    gets: list[int] = []
    sets: list[int] = []
    for s in samples:
        g, w = s.taken()
        gets += g
        sets += w
    return {'get_us': percentiles(gets), 'set_us': percentiles(sets)}


def make(variant: str, directory: Path) -> Any:
    import importlib

    module, cls, kwargs = VARIANTS[variant]
    return getattr(importlib.import_module(module), cls)(str(directory), timeout=TIMEOUT, **kwargs)


def import_variant(variant: str) -> None:
    import importlib

    importlib.import_module(VARIANTS[variant][0])


def check_release_build() -> None:
    import diskcache_oxide

    flag = getattr(diskcache_oxide, '_DEBUG_BUILD', None)
    if flag is None:
        flag = getattr(getattr(diskcache_oxide, '_core', None), '_DEBUG_BUILD', False)
    if flag:
        raise SystemExit('refusing to benchmark a debug build of diskcache_oxide; rebuild in release mode')


# ---------------------------------------------------------------------------------- memory


def rss_bytes() -> float:
    """This process's current resident set size in bytes (NaN where we can't read it)."""
    if sys.platform.startswith('linux'):
        with open('/proc/self/statm') as f:
            return float(int(f.read().split()[1]) * os.sysconf('SC_PAGE_SIZE'))
    if sys.platform == 'darwin':
        out = subprocess.run(['ps', '-o', 'rss=', '-p', str(os.getpid())], capture_output=True, text=True, check=True)
        return float(int(out.stdout.strip()) * 1024)
    return math.nan


def peak_rss_bytes() -> float:
    """This process's peak resident set size in bytes: ru_maxrss is bytes on macOS, KiB on Linux."""
    if sys.platform == 'win32':
        return math.nan
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return float(peak if sys.platform == 'darwin' else peak * 1024)


def memory_report(base: float) -> dict[str, float]:
    return {'rss_mib': (rss_bytes() - base) / MIB, 'peak_mib': (peak_rss_bytes() - base) / MIB}


# ---------------------------------------------------------------------------------- the mixes


def mix_tag(mix: str) -> str:
    """'' for the headline mix (so its case names stay as they were), '-90to10' for a stress mix."""
    return '' if mix == HEADLINE else '-' + mix.replace(':', 'to')


def size_tag(size: str) -> str:
    return '' if size == DEFAULT_SIZE else f'-v{size}'


def case_name(case: Case) -> str:
    kind, dist, mix, n, size = case
    return f'{kind}-{dist}{mix_tag(mix)}{size_tag(size)}-{n}'


def mix_of(name: str) -> str:
    for mix in MIXES:
        if mix != HEADLINE and mix_tag(mix) + '-' in name:
            return mix
    return HEADLINE


def size_of(name: str) -> str:
    for size in SIZES:
        if size != DEFAULT_SIZE and size_tag(size) + '-' in name:
            return size
    return DEFAULT_SIZE


def key_names(keys: int) -> list[str]:
    return [''.join(('t:', str(i))) for i in range(keys)]


def key_plan(names: list[str], dist: str, every: int, seed: int) -> Plan:
    """A cycling plan of (key, is_write, is_timed) triples, built per thread so nothing Python-level is shared.

    About one get in 61 is timed, and one operation in 100 when it's a write (every write in the 99:1
    mix, every 10th in 90:10, every 50th in 50:50), so the timing costs well under 1% of the loop.
    """
    rng = random.Random(seed)
    keys = len(names)
    if dist == 'zipf':
        weights = list(itertools.accumulate(1 / (rank + 1) for rank in range(keys)))
        picks = rng.choices(range(keys), cum_weights=weights, k=PLAN_LEN)
    else:
        picks = [rng.randrange(keys) for _ in range(PLAN_LEN)]
    plan = []
    for i, k in enumerate(picks):
        write = i % every == 0
        plan.append((names[k], write, i % 100 == 0 if write else i % GET_SAMPLE_EVERY == 7))
    return plan


def dataset_keys(size: str, keys: int, quick: bool) -> int:
    """1 KB values get `keys` keys; larger values get a fixed byte budget, so filling stays quick."""
    if size == DEFAULT_SIZE:
        return keys
    return (QUICK_SIZING_BYTES if quick else SIZING_BYTES) // SIZES[size]


def fill_marker(directory: Path | str) -> Path:
    directory = Path(directory)
    return directory.with_name(directory.name + '.filled')


def filled_keys(directory: Path | str) -> int:
    return json.loads(fill_marker(directory).read_text())['keys']


def ensure_filled(variant: str, directory: Path, keys: int, value: str) -> None:
    """Fill the variant's database once per run (in this process, which never measures memory)."""
    marker = fill_marker(directory)
    if marker.exists() and json.loads(marker.read_text()) == {'keys': keys, 'value': len(value)}:
        return
    started = time.perf_counter()
    directory.parent.mkdir(parents=True, exist_ok=True)
    cache = make(variant, directory)
    for i in range(keys):
        cache.set(f't:{i}', value)
    cache.close()
    marker.write_text(json.dumps({'keys': keys, 'value': len(value)}))
    print(
        f'  filled {variant}: {keys:,} keys x {len(value):,} B in {time.perf_counter() - started:.1f} s',
        file=sys.stderr,
    )


def run_loop(cache: Any, plan: Plan, value: str, stop: Callable[[], bool], samples: Samples) -> int:
    get, set_ = cache.get, cache.set
    clock = time.perf_counter_ns
    gets, sets = samples.gets, samples.sets
    cap = len(gets)
    n_gets = n_sets = 0
    ops = 0
    # Check the clock every STOP_CHECK operations, so a slow variant doesn't overrun its window by a whole plan.
    chunks = [plan[i : i + STOP_CHECK] for i in range(0, len(plan), STOP_CHECK)]
    for chunk in itertools.cycle(chunks):
        if stop():
            break
        for key, write, timed in chunk:
            if timed:
                if write:
                    t0 = clock()
                    set_(key, value)
                    if n_sets < cap:
                        sets[n_sets] = clock() - t0
                        n_sets += 1
                else:
                    t0 = clock()
                    get(key)
                    if n_gets < cap:
                        gets[n_gets] = clock() - t0
                        n_gets += 1
            elif write:
                set_(key, value)
            else:
                get(key)
        ops += len(chunk)
    samples.n_gets, samples.n_sets = n_gets, n_sets
    return ops


def run_threads(cache: Any, plans: list[Plan], value: str, seconds: float, samples: list[Samples]) -> float:
    threads = len(plans)
    counts = [0] * threads
    stop = threading.Event()
    barrier = threading.Barrier(threads + 1)

    def worker(n: int) -> None:
        barrier.wait()
        counts[n] = run_loop(cache, plans[n], value, stop.is_set, samples[n])

    pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for t in pool:
        t.start()
    barrier.wait()
    start = time.perf_counter()
    time.sleep(seconds)
    stop.set()
    for t in pool:
        t.join()
    return sum(counts) / (time.perf_counter() - start)


def thread_child(variant: str, directory: str, case: Case, timing: tuple[float, float], out: Any) -> None:
    """One threads case in a fresh process: baseline, open, warm, measure, report."""
    _, dist, mix, threads, size = case
    warm, seconds = timing
    value = 'v' * SIZES[size]
    import_variant(variant)
    names = key_names(filled_keys(directory))
    plans = [key_plan(names, dist, MIXES[mix], n) for n in range(threads)]
    del names
    samples = [Samples(seconds) for _ in range(threads)]
    base = rss_bytes()
    cache = make(variant, Path(directory))
    run_threads(cache, plans, value, warm, samples)  # The warm-up's samples are overwritten.
    rate = run_threads(cache, plans, value, seconds, samples)
    report = memory_report(base)
    cache.close()
    out.put({'ops': rate, **report, **latency_report(samples)})


def threads_case(variant: str, directory: Path, case: Case, timing: tuple[float, float]) -> dict[str, Any]:
    ctx = multiprocessing.get_context('spawn')
    out = ctx.Queue()
    child = ctx.Process(target=thread_child, args=(variant, str(directory), case, timing, out))
    child.start()
    result = out.get(timeout=120 + sum(timing))
    child.join()
    return result


def proc_child(
    variant: str, directory: str, case: Case, seed: int, timing: tuple[float, float], start: Any, out: Any
) -> None:
    _, dist, mix, _, size = case
    warm, seconds = timing
    value = 'v' * SIZES[size]
    import_variant(variant)
    names = key_names(filled_keys(directory))
    plan = key_plan(names, dist, MIXES[mix], seed)
    del names
    samples = Samples(seconds)
    base = rss_bytes()
    cache = make(variant, Path(directory))
    warm_end = time.perf_counter() + warm
    run_loop(cache, plan, value, lambda: time.perf_counter() >= warm_end, samples)
    start.wait(timeout=120)  # Every process has opened and warmed up before any clock starts.
    begin = time.perf_counter()
    end = begin + seconds
    ops = run_loop(cache, plan, value, lambda: time.perf_counter() >= end, samples)
    elapsed = time.perf_counter() - begin
    report = memory_report(base)
    cache.close()
    gets, sets = samples.taken()
    out.put({'ops': ops / elapsed, **report, 'gets': gets, 'sets': sets})


def procs_case(variant: str, directory: Path, case: Case, timing: tuple[float, float]) -> dict[str, Any]:
    procs = case[3]
    ctx = multiprocessing.get_context('spawn')
    out = ctx.Queue()
    start = ctx.Barrier(procs + 1)
    children = [
        ctx.Process(target=proc_child, args=(variant, str(directory), case, 100 + n, timing, start, out))
        for n in range(procs)
    ]
    for p in children:
        p.start()
    start.wait(timeout=120)
    results = [out.get(timeout=120 + sum(timing)) for _ in children]
    for p in children:
        p.join()
    return {
        'ops': sum(r['ops'] for r in results),
        'rss_mib': statistics.median(r['rss_mib'] for r in results),
        'peak_mib': statistics.median(r['peak_mib'] for r in results),
        'get_us': percentiles([ns for r in results for ns in r['gets']]),
        'set_us': percentiles([ns for r in results for ns in r['sets']]),
    }


# ---------------------------------------------------------------------------------- small reads


def small_key(i: int) -> str:
    return f'https://talkpython.fm/static/img/{i:05d}/cover.webp?{i}'


def small_fill(cache: Any) -> None:
    for i in range(SMALL_KEYS):
        cache.set(small_key(i), f'{i:06x}')


def small_case(caches: list[Any], threads: int) -> float:
    """Reads per second with `threads` threads, thread i reading from caches[i % len(caches)]."""
    lists = [[small_key(i) for i in random.Random(t).choices(range(SMALL_KEYS), k=SMALL_OPS)] for t in range(threads)]
    barrier = threading.Barrier(threads + 1)

    def worker(n: int) -> None:
        get = caches[n % len(caches)].get
        keys = lists[n]
        barrier.wait()
        for k in keys:
            get(k)

    pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for t in pool:
        t.start()
    barrier.wait()
    start = time.perf_counter()
    for t in pool:
        t.join()
    return threads * SMALL_OPS / (time.perf_counter() - start)


def small_cases(variant: str, tmp: Path, wanted: Callable[[str], bool]) -> dict[str, float]:
    results: dict[str, float] = {}
    shared = make(variant, tmp / 'small-shared')
    small_fill(shared)
    # Three timed repetitions inside the process, after one warm-up, report the median.
    plan: list[tuple[str, list[Any], int]] = [('small-1t', [shared], 1), ('small-4t-shared', [shared], 4)]
    separate: list[Any] = []
    if wanted('small-4t-separate'):
        separate = [make(variant, tmp / f'small-own-{i}') for i in range(4)]
        for c in separate:
            small_fill(c)
        plan.append(('small-4t-separate', separate, 4))
    for name, caches, threads in plan:
        if not wanted(name):
            continue
        small_case(caches, threads)
        results[name] = statistics.median(small_case(caches, threads) for _ in range(3))
    for c in [shared, *separate]:
        c.close()
    return results


# ---------------------------------------------------------------------------------- the grid


def parse_counts(text: str) -> tuple[int, ...]:
    return () if text.strip().lower() in ('', 'none') else tuple(int(n) for n in text.split(','))


def wanted(args: argparse.Namespace, name: str) -> bool:
    return not args.only or any(name.startswith(prefix) for prefix in args.only)


def selected_groups(args: argparse.Namespace) -> list[str]:
    names = args.group or ['scaling']
    return list(GROUPS) if 'all' in names else list(dict.fromkeys(names))


def group_cases(args: argparse.Namespace, group: Group) -> list[Case]:
    """The group's (kind, dist, mix, count, size) cases that the arguments select, in run order."""
    counts, stress = group.counts, group.stress_counts
    if args.full:
        counts = stress = THREAD_COUNTS
    if args.counts is not None:
        counts = parse_counts(args.counts)
    if args.stress_counts is not None:
        stress = parse_counts(args.stress_counts)
    if args.quick:
        counts, stress = tuple(n for n in (1, 4) if counts), tuple(n for n in (4,) if stress)
    cases: list[Case] = []
    for size in group.sizes:
        for dist in DISTS:
            for kind in group.kinds:
                for mix in group.mixes:
                    for n in counts if mix == HEADLINE else stress:
                        case = (kind, dist, mix, n, size)
                        keep = mix in args.mixes.split(',') and dist in args.dists.split(',')
                        if keep and size in args.sizes.split(',') and wanted(args, case_name(case)):
                            cases.append(case)
    return cases


def group_variants(args: argparse.Namespace, group: Group) -> list[str]:
    return list(args.variant_list) if args.variant_list else list(group.variants)


def run_plan(args: argparse.Namespace) -> tuple[dict[str, list[Case]], set[str]]:
    """Each variant's cases across the selected groups (each once), and the variants that run the small cases."""
    plan: dict[str, list[Case]] = {}
    small: set[str] = set()
    for name in selected_groups(args):
        group = GROUPS[name]
        cases = group_cases(args, group)
        for variant in group_variants(args, group):
            mine = plan.setdefault(variant, [])
            skip_small = variant in group.large_values_only
            mine.extend(c for c in cases if c not in mine and not (skip_small and c[4] == DEFAULT_SIZE))
            if group.small and wanted(args, 'small'):
                small.add(variant)
    return {v: cases for v, cases in plan.items() if cases or v in small}, small


def check_grid_args(args: argparse.Namespace) -> None:
    for option, choices in (('mixes', MIXES), ('dists', DISTS), ('sizes', SIZES)):
        for value in getattr(args, option).split(','):
            if value not in choices:
                raise SystemExit(f'unknown --{option} value {value!r}; choose from {", ".join(choices)}')
    for name in args.group or []:
        if name != 'all' and name not in GROUPS:
            raise SystemExit(f'unknown group {name!r}; choose from all, {", ".join(GROUPS)}')


# ---------------------------------------------------------------------------------- worker


def worker_main(args: argparse.Namespace) -> None:
    check_release_build()
    timing = (0.1, 0.3) if args.quick else (WARM_SECONDS, args.seconds)
    job = json.loads(Path(args.job).read_text())
    results: dict[str, Any] = {}
    for raw in job['cases']:
        case: Case = (raw[0], raw[1], raw[2], int(raw[3]), raw[4])
        size = case[4]
        directory = Path(args.data_dir) / size
        ensure_filled(args.variant, directory, dataset_keys(size, args.keys, args.quick), 'v' * SIZES[size])
        run = threads_case if case[0] == 'threads' else procs_case
        results[case_name(case)] = run(args.variant, directory, case, timing)
    if job['small']:
        with tempfile.TemporaryDirectory(prefix='dco-fanout-small-') as tmp_name:
            results.update(small_cases(args.variant, Path(tmp_name), lambda name: wanted(args, name)))
    Path(args.out).write_text(json.dumps(results))


# ---------------------------------------------------------------------------------- driver


def loadavg() -> float:
    # Windows has no load average.
    return math.nan if sys.platform == 'win32' else os.getloadavg()[0]


def busy_processes() -> list[str]:
    out = subprocess.run(['ps', '-Ao', 'comm'], capture_output=True, text=True).stdout.splitlines()
    return sorted({line.rsplit('/', 1)[-1] for line in out if line.rsplit('/', 1)[-1] in ('cargo', 'rustc', 'pytest')})


def power_source() -> str:
    if sys.platform != 'darwin':
        return 'unknown'
    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True, text=True, timeout=5).stdout
    except OSError, subprocess.SubprocessError:
        return 'unknown'
    return 'ac' if 'AC Power' in out else 'battery' if 'Battery Power' in out else 'unknown'


def spread(values: list[float]) -> dict[str, Any]:
    """Median, min, max and coefficient of variation (sample stdev / mean, 0 for one round) across rounds."""
    real = [v for v in values if not math.isnan(v)]
    if not real:
        return {'median': math.nan, 'min': math.nan, 'max': math.nan, 'cv': math.nan, 'samples': values}
    mean = statistics.fmean(real)
    cv = statistics.stdev(real) / mean if len(real) > 1 and mean else 0.0
    return {'median': statistics.median(real), 'min': min(real), 'max': max(real), 'cv': cv, 'samples': values}


def driver_main(args: argparse.Namespace) -> None:
    check_release_build()
    import diskcache
    import diskcache_oxide

    plan, small = run_plan(args)
    variants = list(plan)
    if not variants:
        raise SystemExit('nothing selected')
    gil_disabled = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
    python = platform.python_version() + ('t' if gil_disabled else '')
    total = sum(len(cases) for cases in plan.values())
    print(
        f'groups {", ".join(selected_groups(args))}: {len(variants)} variants, {total} threads/procs runs per round'
        f'{f", small on {len(small)}" if small else ""}, {args.rounds} round(s)',
        file=sys.stderr,
    )
    samples: dict[str, dict[str, list[Any]]] = {v: {} for v in variants}
    loads: list[dict[str, Any]] = []
    run_started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='dco-fanout-run-') as tmp:
        for variant in variants:
            Path(tmp, f'{variant}.job.json').write_text(json.dumps({'cases': plan[variant], 'small': variant in small}))
        for r in range(args.rounds):
            order = variants[r % len(variants) :] + variants[: r % len(variants)]
            loads.append({'round': r + 1, 'load_before': loadavg(), 'busy': busy_processes()})
            for variant in order:
                out = Path(tmp) / f'{variant}-{r}.json'
                cmd = [sys.executable, '-m', 'bench.fanout', '--worker', '--variant', variant, '--out', str(out)]
                cmd += ['--job', str(Path(tmp, f'{variant}.job.json')), '--data-dir', str(Path(tmp, 'data', variant))]
                cmd += ['--seconds', str(args.seconds), '--keys', str(args.keys)]
                cmd += ['--quick'] if args.quick else []
                for prefix in args.only or []:
                    cmd += ['--only', prefix]
                subprocess.run(cmd, check=True)
                for case, value in json.loads(out.read_text()).items():
                    samples[variant].setdefault(case, []).append(value)
                minutes = (time.perf_counter() - run_started) / 60
                print(
                    f'round {r + 1}/{args.rounds}: {variant} done (load {loadavg():.1f}, {minutes:.1f} min)',
                    file=sys.stderr,
                )
            loads[-1]['load_after'] = loadavg()

    summary: dict[str, dict[str, dict[str, Any]]] = {}
    for variant, by_case in samples.items():
        summary[variant] = {}
        for case, values in by_case.items():
            if isinstance(values[0], dict):
                entry = spread([v['ops'] for v in values])
                entry['mix'] = mix_of(case)
                entry['value_bytes'] = SIZES[size_of(case)]
                entry['rss_growth_mib'] = spread([v['rss_mib'] for v in values])
                entry['peak_growth_mib'] = spread([v['peak_mib'] for v in values])
                entry['latency_us'] = {
                    op: {q: spread([v[f'{op}_us'][q] for v in values]) for q in ('p50', 'p99', 'p999')}
                    | {'n': [v[f'{op}_us']['n'] for v in values]}
                    for op in ('get', 'set')
                }
            else:
                entry = spread(values)
            summary[variant][case] = entry
    groups = {
        name: {
            'about': GROUPS[name].about,
            'variants': group_variants(args, GROUPS[name]),
            'cases': [case_name(c) for c in group_cases(args, GROUPS[name])]
            + (['small-1t', 'small-4t-shared', 'small-4t-separate'] if GROUPS[name].small and small else []),
        }
        for name in selected_groups(args)
    }
    meta = {
        'python': python,
        'gil_enabled_at_runtime': sys._is_gil_enabled() if hasattr(sys, '_is_gil_enabled') else True,
        'machine': args.machine,
        'platform': platform.platform(),
        'cpu_count': os.cpu_count(),
        'power': power_source(),
        'oxide_version': diskcache_oxide.__version__,
        'diskcache_version': diskcache.__version__,
        'rounds': args.rounds,
        'seconds': 0.3 if args.quick else args.seconds,
        'warm_seconds': 0.1 if args.quick else WARM_SECONDS,
        'quick': args.quick,
        'full': args.full,
        'only': args.only,
        'keys': QUICK_KEYS if args.quick else args.keys,
        'value_bytes': SIZES[DEFAULT_SIZE],
        'sizing_keys': {s: dataset_keys(s, args.keys, args.quick) for s in SIZES if s != DEFAULT_SIZE},
        'mix': f'1 write in {MIXES[HEADLINE]} operations',
        'mixes': {mix: f'1 write in {every} operations' for mix, every in MIXES.items()},
        'headline_mix': HEADLINE,
        'groups': groups,
        'variants': {v: VARIANTS[v][1] + repr(VARIANTS[v][2]) for v in variants},
        'memory': 'MiB over the post-import RSS baseline of each case process; rss = current after the window, '
        'peak = ru_maxrss; procs cases report the median process',
        'loads': loads,
        'elapsed_minutes': (time.perf_counter() - run_started) / 60,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    out_path = args.out or RESULTS_DIR / f'fanout-{python}-{args.machine}{"-quick" if args.quick else ""}.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({'meta': meta, 'results': summary}, indent=2) + '\n')
    print(f'\nPython {python}, {meta["keys"]:,} keys x 1 KB, medians over {args.rounds} round(s)')
    for name, info in groups.items():
        (print_detail if GROUPS[name].detail else print_tables)(name, info, summary)
    print(f'\nWrote {out_path} ({meta["elapsed_minutes"]:.1f} min)')


def case_order(case: str) -> tuple[int, int, str, int]:
    """Group by value size and mix (headline first), then kind and distribution, then count."""
    head, _, count = case.rpartition('-')
    return list(SIZES).index(size_of(case)), list(MIXES).index(mix_of(case)), head, int(count) if count.isdigit() else 0


def print_tables(group: str, info: dict[str, Any], summary: dict[str, dict[str, dict[str, Any]]]) -> None:
    variants = info['variants']
    cases = sorted(info['cases'], key=case_order)
    width = max(13, *(len(v) + 1 for v in variants))
    header = f'{"case":30}' + ''.join(f'{v:>{width}}' for v in variants)

    def section(title: str, cell: Callable[[dict[str, Any]], str], rows: list[str]) -> None:
        print(f'\n[{group}: {info["about"]}] {title}\n{header}')
        current = None
        for case in rows:
            key = (size_of(case), mix_of(case))
            if key != current and not case.startswith('small'):
                current = key
                label = 'headline' if key[1] == HEADLINE else 'stress case, write-heavy'
                print(f'-- {key[1]} ({label}), {SIZES[key[0]]:,} B values')
            row = f'{case:30}'
            for v in variants:
                entry = summary.get(v, {}).get(case)
                row += f'{cell(entry) if entry else "-":>{width}}'
            print(row)

    section('Throughput, ops/s (spread = (max - min) / median across rounds)', ops_cell, cases)
    memory_rows = [c for c in cases if not c.startswith('small')]
    if memory_rows:
        section(
            'Memory, RSS growth per process in MiB: current after the window / peak (procs: the median process)',
            memory_cell,
            memory_rows,
        )


def us(value: float) -> str:
    return '-' if math.isnan(value) else f'{value:.1f}' if value < 100 else f'{value:,.0f}'


def print_detail(group: str, info: dict[str, Any], summary: dict[str, dict[str, dict[str, Any]]]) -> None:
    """One block per case, one row per variant: throughput spread, memory and latency percentiles.

    Flags mark a row against its neighbours in the same case: UNSTABLE when its throughput cv across
    rounds is over 5% and over twice the case's median cv, TAIL-GET / TAIL-SET when its p99.9 is over
    3x the case's median p99.9. Latencies are each round's percentile, median over rounds, in us.
    """
    variants = info['variants']
    print(f'\n[{group}: {info["about"]}]')
    print('kops/s: median, min-max over rounds, cv = stdev/mean; memory: RSS growth MiB, current / peak')
    header = (
        f'  {"variant":21}{"kops/s":>8}{"min-max":>14}{"cv":>6}{"MiB":>12}'
        f'{"get p50":>9}{"p99":>8}{"p99.9":>8}{"set p50":>9}{"p99":>8}{"p99.9":>8}  flags'
    )
    for case in sorted((c for c in info['cases'] if not c.startswith('small')), key=case_order):
        rows = [(v, summary[v][case]) for v in variants if case in summary.get(v, {})]
        if not rows:
            continue
        mix, size = mix_of(case), size_of(case)
        label = 'headline' if mix == HEADLINE else 'stress case, write-heavy'
        print(f'\n{case}  ({mix} {label}, {SIZES[size]:,} B values)\n{header}')

        def middle(pick: Callable[[dict[str, Any]], float], rows: list[tuple[str, dict[str, Any]]] = rows) -> float:
            values = [x for x in (pick(e) for _, e in rows) if not math.isnan(x)]
            return statistics.median(values) if values else math.nan

        mid_cv = middle(lambda e: e['cv'])
        mid_get = middle(lambda e: e['latency_us']['get']['p999']['median'])
        mid_set = middle(lambda e: e['latency_us']['set']['p999']['median'])
        for variant, e in rows:
            lat = {op: {q: e['latency_us'][op][q]['median'] for q in ('p50', 'p99', 'p999')} for op in ('get', 'set')}
            flags = []
            if e['cv'] > 0.05 and e['cv'] > 2 * mid_cv:
                flags.append('UNSTABLE')
            if lat['get']['p999'] > 3 * mid_get:
                flags.append('TAIL-GET')
            if lat['set']['p999'] > 3 * mid_set:
                flags.append('TAIL-SET')
            spread_text = f'{e["min"] / 1000:,.0f}-{e["max"] / 1000:,.0f}'
            mem = f'{e["rss_growth_mib"]["median"]:.1f}/{e["peak_growth_mib"]["median"]:.1f}'
            print(
                f'  {variant:21}{e["median"] / 1000:>8,.0f}{spread_text:>14}{e["cv"] * 100:>5.1f}%{mem:>12}'
                f'{us(lat["get"]["p50"]):>9}{us(lat["get"]["p99"]):>8}{us(lat["get"]["p999"]):>8}'
                f'{us(lat["set"]["p50"]):>9}{us(lat["set"]["p99"]):>8}{us(lat["set"]["p999"]):>8}'
                f'  {" ".join(flags)}'
            )


def ops_cell(entry: dict[str, Any]) -> str:
    median = entry['median']
    pct = (entry['max'] - entry['min']) / median * 100 if median else 0.0
    return f'{median / 1000:,.0f}k {pct:2.0f}%'


def memory_cell(entry: dict[str, Any]) -> str:
    if 'rss_growth_mib' not in entry:
        return '-'
    return f'{entry["rss_growth_mib"]["median"]:.1f} / {entry["peak_growth_mib"]["median"]:.1f}'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--group',
        action='append',
        metavar='NAME',
        help=f'Case group to run (repeatable): {", ".join(GROUPS)} or all (default scaling).',
    )
    parser.add_argument('--rounds', type=int, help='Interleaved rounds (default 3, or 1 with --quick).')
    parser.add_argument('--seconds', type=float, default=SECONDS, help='Measured seconds per threads or procs case.')
    parser.add_argument('--quick', action='store_true', help='Smaller, shorter run for smoke-testing the script.')
    parser.add_argument('--full', action='store_true', help='Every selected mix at 1, 2, 4 and 8 threads/processes.')
    parser.add_argument('--keys', type=int, default=KEYS, help=f'Keys in each 1 KB database (default {KEYS:,}).')
    parser.add_argument('--mixes', default=','.join(MIXES), help='Only these mixes (comma-separated).')
    parser.add_argument('--dists', default=','.join(DISTS), help='Only these key distributions: uniform, zipf.')
    parser.add_argument('--sizes', default=','.join(SIZES), help='Only these value sizes: 1k, 16k, 64k.')
    parser.add_argument('--counts', help='Counts for the 99:1 mix in every group, e.g. 1,2,4,8, or none.')
    parser.add_argument('--stress-counts', help='Counts for the 90:10 and 50:50 mixes in every group, or none.')
    parser.add_argument('--machine', default=platform.node().split('.')[0] or 'unknown')
    parser.add_argument('--out', type=Path, help='Override the output path.')
    parser.add_argument(
        '--only',
        action='append',
        metavar='PREFIX',
        help='Only cases starting with this prefix (repeatable): threads, procs, threads-zipf, small, ...',
    )
    parser.add_argument(
        '--variant-list',
        nargs='+',
        choices=list(VARIANTS),
        help="Variants to run in each selected group, in place of the group's own.",
    )
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--variant', choices=sorted(VARIANTS), help=argparse.SUPPRESS)
    parser.add_argument('--data-dir', help=argparse.SUPPRESS)
    parser.add_argument('--job', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.rounds is None:
        args.rounds = 1 if args.quick else 3
    check_grid_args(args)
    if args.worker:
        worker_main(args)
    else:
        driver_main(args)


if __name__ == '__main__':
    main()
