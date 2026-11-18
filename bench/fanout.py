"""FanoutCache benchmark (plan 13, step 13i; extended for plans 29 and 30): what sharding buys, and its memory cost.

Usage (from the repo root, on AC power, on a quiet machine):
    venv/bin/python -m bench.fanout                      # 3.14: the scaling group, 3 interleaved rounds
    .venv-ft/bin/python -m bench.fanout                  # 3.14t
    venv/bin/python -m bench.fanout --group all          # every group (see below for how long each takes)
    venv/bin/python -m bench.fanout --group pagecache --group sizing
    venv/bin/python -m bench.fanout --group sizing2      # the page-cache default across value sizes
    venv/bin/python -m bench.fanout --quick --group all  # smoke test, one short round
    venv/bin/python -m bench.fanout --only small --rounds 5
    venv/bin/python -m bench.fanout --mixes 99:1 --counts 1,2,4,8 --stress-counts none   # the plan-13 grid

Groups (--group, repeatable; default scaling). A variant in several groups runs each case once.
Times are for 3 rounds at the defaults on an M-series Mac; --group all takes about 55 minutes.
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
  cases, and FanoutCache(32) at 1 KB (it runs 16 KiB and 64 KiB only). The 16 KiB and 64 KiB cases
  are the tail cases: they time every write and one get in 7, and their measured window runs past
  --seconds until the threads have taken TAIL_SAMPLES (1,000) get and set samples between them, up to
  TAIL_MAX_SECONDS (10 s), so their p99.9 rests on at least 1,000 samples (a slow variant at 99:1
  and 1 thread takes about 3 s). ops/s is still operations over the window's actual length.
- sizing2 (deciding plan 30's page-cache default across value sizes, about 23 minutes): oxide Cache
  at 256 (the default), 128 and 64 pages; FanoutCache(8) at the default (256), 64 and 32;
  FanoutCache(4) at 64. 4 KiB, 8 KiB and mixed values (the 'mix' size, below); 99:1 and 90:10, each
  at 1 and 8; uniform and Zipf; threads and processes (a web garden). Every case is a tail case
  (values over 1 KB): in a procs case each process runs past --seconds until it holds its share of
  TAIL_SAMPLES (1,000 / N) get and set samples, up to TAIL_MAX_SECONDS, so p99.9 rests on at least
  1,000 samples there too; its ops/s is over its own window. Datasets hold SIZING_BYTES, as in sizing.
  The 'mix' size is a web cache's spread: 50% of keys 1 KiB, 30% 4 KiB, 15% 16 KiB, 5% 64 KiB
  (65,472, inline), each key's size fixed by a keyed blake2b hash of the key (mix_value_len), so the
  fill and every process agree, and a write sets the key's own size. Its value_bytes is the mean
  (7,472 B), and meta.value_mix records the distribution and the dataset's actual counts and mean.
pagecache, sizing and sizing2 print one row per case and variant: ops/s median, min-max and cv across rounds,
memory, and sampled get and set latency (p50, p99, p99.9) with the fewest samples any round took,
and UNSTABLE, TAIL-GET and TAIL-SET flags on a row that stands out from the other variants in the
same case (see print_detail). Every case records the latency, whatever group it's in; outside the
tail cases about one get in 61 and one operation in 100 that's a write are timed with
perf_counter_ns, into buffers allocated before the memory baseline. A percentile needs enough samples
to mean anything: p99 is NaN (printed '-') under 100 samples and p99.9 under 1,000, so the 1 KB
cases' set p99.9 is usually '-' at 99:1 (about 1,000 writes a second over all threads, 1 in 100 timed).
Outside every group (pass --variant-list, which replaces each selected group's variants): oxide
FanoutCache(8) at 64 pages in the scaling grid, max_connections=32 on Cache and FanoutCache(8), to
tell a connection-budget limit from a shard one, and FanoutCache(8) with an explicit
sqlite_cache_size=256, which keeps today's default measurable once plan 30 splits the default.

Mixes (get:set): 99:1 is the headline. 90:10 and 50:50 are write-heavy stress cases, labelled as
such in the table; they show where sharding the write lock matters. Zipf (s=1.0) keys are the
hot-working-set case, where the page-cache size matters most.

Cases (N is the thread or process count; non-headline mixes and value sizes are in the name):
- threads-{uniform,zipf}[-90to10|-50to50][-v4k|-v8k|-v16k|-v64k|-vmix]-N: N threads in one fresh process sharing one
  handle. 1 KB values over KEYS (default 50,000) keys: a ~50 MB database, larger than any variant's
  page-cache budget (FanoutCache(8)'s default is 16 MiB), and every thread's plan reads all of it
  (Key plans, below). ops/s for the process, and memory (below).
- procs-{uniform,zipf}[-90to10|-50to50][-v4k|-v8k|-vmix]-N: the same mix from N spawned processes, one thread each,
  each opening the directory itself (a web garden). ops/s summed over the processes; memory per
  process, the median across them (not summed).
- small-1t / small-4t-shared / small-4t-separate: scale_probe's free-threaded small-read case (700
  URL-shaped keys, 6-byte values, reads only, 4 threads) on one shared handle, or on four separate
  instances of the variant (one directory each). This is what FanoutCache exists for. No memory.
--counts and --stress-counts replace every group's counts for the 99:1 mix and the others (`none`
drops them), --full runs 1, 2, 4 and 8 everywhere, --mixes, --dists and --sizes narrow the grid, and
--only PREFIX filters case names on top (threads, procs-zipf, threads-uniform-50to50, small...).

Key plans. Each thread (or process) cycles its own plan of plan_len(keys) operations: at least
PLAN_MIN (10,000) and at least twice the database's keys, a whole number of passes over them (100,000
for 50,000 keys). Uniform plans are shuffled passes, each key once per pass, so every key is read
equally often and the plan covers the whole database; Zipf plans draw from every key, weighted by
rank. The warm-up starts at the plan's first operation and the measured window at its midpoint, so
the window doesn't just replay what the warm-up read. meta.plans records each dataset's plan length
and distinct keys per plan (thread 0's, computed).

Memory. Every threads and procs case runs in fresh processes against a database filled beforehand
by another process, so filling never shows up in its numbers. A case process imports the variant's
module and builds its key plans, takes its memory as the baseline, then opens the cache, warms up
for WARM_SECONDS, and runs the measured window. It reports, in MiB over that baseline: `rss`
(current RSS just after the window, before closing the cache), `peak` (ru_maxrss at the same point,
which is bytes on macOS and KiB on Linux) and `private` (process-private memory at the same point:
phys_footprint from proc_pid_rusage on macOS, Private_Dirty plus Swap from /proc/self/smaps_rollup on
Linux). RSS counts resident pages of mapped files, and stock diskcache maps up to 64 MiB of its
database (sqlite_mmap_size), so its RSS growth is mostly the database file: shared with every
process mapping it and reclaimable. `private` leaves that out (a file mapped read-only stays clean),
so it's the number to compare across variants. All three include the threads' own stacks and Python
objects, alike for every variant; the differences between variants are the cache's. NaN where we
can't read them (Windows; `private` on other platforms too).

Each round runs every variant in a fresh worker process (the order rotates each round, so drift hits
all of them alike), and each variant's databases are filled once per run, before its first round.
The driver reports the median over rounds and the min-max spread. Only compare results from the same
machine. Writes bench/results/fanout-<python>-<machine>.json after every round: per variant and
case, the ops/s `median`/`min`/`max`/`samples` as before, plus `cv`, `mix`, `value_bytes`,
`rss_growth_mib`, `peak_growth_mib`, `private_growth_mib` and `latency_us` (get and set, each
p50/p99/p999 with its own median, min, max, cv and samples, and the sample counts `n`), and
`errors` when a round failed. A case that fails (a crashed case process, an exception, a worker
that dies) is recorded as NaN with its error for that round, and the run goes on; meta.errors lists
them. Results from before this extension used 1,000 keys, the 99:1 mix only and one process for all
of a variant's cases; `meta.keys` says which. Results from before meta.plans cycled a 10,000-pick
plan (about 9,000 distinct keys) and measured from the same start as the warm-up.

The driver raises its soft RLIMIT_NOFILE to the hard limit before starting (every worker and case
process inherits it) and warns below 4,096: stock diskcache's FanoutCache opens a connection per
thread per shard, and under the 256 a non-interactive ssh shell gets on macOS it fails partway
through (bench/fanout_fds.py measures where). meta.nofile records the limit.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import multiprocessing
import os
import platform
import queue
import random
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import traceback
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

RESULTS_DIR = Path(__file__).parent / 'results'
THREAD_COUNTS = (1, 2, 4, 8)
KEYS = 50_000
QUICK_KEYS = 5_000
SIZING_BYTES = 64 * 2**20  # Values per sizing database: 4x the 16 MiB page cache of FanoutCache(8)'s default.
QUICK_SIZING_BYTES = 8 * 2**20
PLAN_MIN = 10_000  # Operations per plan, at least; plan_len() also makes it at least two passes over the keys.
STOP_CHECK = 500
GET_SAMPLE_EVERY = 61  # Time about one get in 61 (prime, so it never lines up with the writes).
TAIL_GET_SAMPLE_EVERY = 7  # In the tail cases (values over 1 KB), one get in 7, and every write.
TAIL_SAMPLES = 1_000  # A tail case's window runs until the threads hold this many get and set samples ...
TAIL_MAX_SECONDS = 10.0  # ... or for this long, whichever comes first (never shorter than --seconds).
SAMPLES_PER_SECOND = 40_000  # Latency buffer per thread and kind; past it, samples stop (only at > 2.4M gets/s).
MIN_P99 = 100  # Fewer samples than these and the percentile is NaN: it would be the max, or near it.
MIN_P999 = 1_000
FD_WARN = 4_096
TIMEOUT = 10.0
SECONDS = 1.0
WARM_SECONDS = 0.2
MIB = 2**20

# Mix name -> one write in every N operations. The first is the headline; the others are stress cases.
MIXES: dict[str, int] = {'99:1': 100, '90:10': 10, '50:50': 2}
HEADLINE = '99:1'
DISTS = ('uniform', 'zipf')
# Value mix (the 'mix' size): (characters, percent of keys), like a web cache's spread of fragments and pages.
# Each key's size is fixed by a keyed hash of the key (mix_value_len), so the fill and every process agree.
VALUE_MIX: tuple[tuple[int, int], ...] = ((1_024, 50), (4_096, 30), (16_384, 15), (65_472, 5))
VALUE_MIX_KEY = b'bench.fanout value mix 1'
MIX_MEAN = sum(chars * pct for chars, pct in VALUE_MIX) // 100  # 7,472
# Value size label -> characters ('mix': the mean). 64k stays under spill::THRESHOLD (65,536 bytes) once encoded,
# so it's inline.
SIZES: dict[str, int] = {'1k': 1_024, '4k': 4_096, '8k': 8_192, '16k': 16_384, '64k': 65_472, 'mix': MIX_MEAN}
DEFAULT_SIZE = '1k'
MIX = 'mix'

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
    'oxide-fanout4-cs64': ('diskcache_oxide', 'FanoutCache', {'shards': 4, 'sqlite_cache_size': 64}),
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
        sizes=('1k', '16k', '64k'),
        detail=True,
        large_values_only=('oxide-fanout32-cs64', 'oxide-fanout32-cs32', 'oxide-fanout32-cs16'),
    ),
    'sizing2': Group(
        about='the page-cache default across value sizes: 4 KiB, 8 KiB and a web-cache mix, threads and processes',
        variants=(*PAGE_CACHE_VARIANTS, 'oxide-fanout4-cs64'),
        kinds=('threads', 'procs'),
        mixes=('99:1', '90:10'),
        counts=(1, 8),
        stress_counts=(1, 8),
        sizes=('4k', '8k', MIX),
        detail=True,
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
        # Updated after every chunk of the loop, so run_threads can see how many a tail case has taken so far.
        self.n_gets = 0
        self.n_sets = 0

    def taken(self) -> tuple[list[int], list[int]]:
        return self.gets[: self.n_gets].tolist(), self.sets[: self.n_sets].tolist()


def percentiles(values: list[int]) -> dict[str, float]:
    """p50, p99 and p99.9 in microseconds (nearest rank), and the sample count.

    p99 is NaN under MIN_P99 samples and p99.9 under MIN_P999: below those, the nearest rank is the
    largest sample or close to it, and the percentile would just be the max under another name.
    """
    n = len(values)
    if not n:
        return {'p50': math.nan, 'p99': math.nan, 'p999': math.nan, 'n': 0}
    values = sorted(values)

    def at(q: float, needed: int) -> float:
        return values[max(0, math.ceil(q * n) - 1)] / 1_000 if n >= needed else math.nan

    return {'p50': at(0.5, 1), 'p99': at(0.99, MIN_P99), 'p999': at(0.999, MIN_P999), 'n': n}


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


def darwin_phys_footprint() -> float:
    """macOS: ri_phys_footprint from proc_pid_rusage(RUSAGE_INFO_V2), the figure Activity Monitor shows as Memory.

    It counts this process's dirty private memory (and its compressed and swapped pages), not clean
    pages of mapped files, so a database file mapped read-only doesn't show up in it.
    """
    import ctypes

    class RusageInfoV2(ctypes.Structure):
        _fields_ = [
            ('ri_uuid', ctypes.c_uint8 * 16),
            *(
                (name, ctypes.c_uint64)
                for name in (
                    'ri_user_time',
                    'ri_system_time',
                    'ri_pkg_idle_wkups',
                    'ri_interrupt_wkups',
                    'ri_pageins',
                    'ri_wired_size',
                    'ri_resident_size',
                    'ri_phys_footprint',
                    'ri_proc_start_abstime',
                    'ri_proc_exit_abstime',
                    'ri_child_user_time',
                    'ri_child_system_time',
                    'ri_child_pkg_idle_wkups',
                    'ri_child_interrupt_wkups',
                    'ri_child_pageins',
                    'ri_child_elapsed_abstime',
                    'ri_diskio_bytesread',
                    'ri_diskio_byteswritten',
                )
            ),
        ]

    libc = ctypes.CDLL(None, use_errno=True)
    info = RusageInfoV2()
    if libc.proc_pid_rusage(os.getpid(), 2, ctypes.byref(info)) != 0:  # 2 = RUSAGE_INFO_V2
        return math.nan
    return float(info.ri_phys_footprint)


def linux_private_bytes() -> float:
    """Linux: Private_Dirty plus Swap from /proc/self/smaps_rollup.

    Private_Clean is left out on purpose: a database file mapped by this process alone lands there,
    which is exactly the file-backed memory this metric exists to exclude.
    """
    fields: dict[str, int] = {}
    with open('/proc/self/smaps_rollup') as f:
        for line in f:
            name, _, rest = line.partition(':')
            parts = rest.split()
            if len(parts) == 2 and parts[1] == 'kB':
                fields[name] = int(parts[0])
    return float((fields.get('Private_Dirty', 0) + fields.get('Swap', 0)) * 1024)


def private_bytes() -> float:
    """This process's private memory in bytes, without resident pages of mapped files (NaN where we can't read it)."""
    try:
        if sys.platform == 'darwin':
            return darwin_phys_footprint()
        if sys.platform.startswith('linux'):
            return linux_private_bytes()
    except OSError, AttributeError:
        pass
    return math.nan


def memory_baseline() -> tuple[float, float]:
    return rss_bytes(), private_bytes()


def memory_report(base: tuple[float, float]) -> dict[str, float]:
    rss, private = base
    return {
        'rss_mib': (rss_bytes() - rss) / MIB,
        'peak_mib': (peak_rss_bytes() - rss) / MIB,
        'private_mib': (private_bytes() - private) / MIB,
    }


# ---------------------------------------------------------------------------------- file descriptors


def raise_fd_limit() -> dict[str, Any]:
    """Raise the soft RLIMIT_NOFILE as far as the hard limit allows; children inherit it. Returns soft and hard.

    macOS reports an unlimited hard limit but refuses a soft one above kern.maxfilesperproc, so on a
    refusal this tries that, then smaller round numbers.
    """
    if sys.platform == 'win32':
        return {'soft': None, 'hard': None}
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    candidates: list[int] = []
    if hard != resource.RLIM_INFINITY:
        candidates.append(hard)
    if sys.platform == 'darwin':
        try:
            out = subprocess.run(['sysctl', '-n', 'kern.maxfilesperproc'], capture_output=True, text=True, timeout=5)
            candidates.append(int(out.stdout.strip()))
        except OSError, ValueError, subprocess.SubprocessError:
            pass
    candidates += [1_048_576, 65_536, 10_240]
    for limit in candidates:
        if soft != resource.RLIM_INFINITY and limit <= soft:
            break
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
            break
        except ValueError, OSError:
            continue
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return {
        'soft': None if soft == resource.RLIM_INFINITY else soft,
        'hard': None if hard == resource.RLIM_INFINITY else hard,
    }


def warn_low_fd_limit(limit: dict[str, Any]) -> None:
    if limit['soft'] is not None and limit['soft'] < FD_WARN:
        print(
            f'warning: RLIMIT_NOFILE soft limit is {limit["soft"]} (hard {limit["hard"]}) after raising it, '
            f"below {FD_WARN:,}: stock diskcache's FanoutCache may fail with too many open files",
            file=sys.stderr,
        )


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


def size_text(size: str) -> str:
    """How a printed header names a case's values."""
    if size == MIX:
        parts = ', '.join(f'{pct}% {chars:,}' for chars, pct in VALUE_MIX)
        return f'mixed values ({parts} B), mean {SIZES[MIX]:,} B'
    return f'{SIZES[size]:,} B values'


def mix_value_len(key: str) -> int:
    """A key's value length in the 'mix' size: a keyed blake2b of the key picks its bucket in VALUE_MIX."""
    roll = int.from_bytes(hashlib.blake2b(key.encode(), digest_size=8, key=VALUE_MIX_KEY).digest(), 'big') % 100
    for chars, pct in VALUE_MIX:
        if roll < pct:
            return chars
        roll -= pct
    return VALUE_MIX[-1][0]


# A case's values: one string for every key, or (the 'mix' size) each key's own.
Values = str | dict[str, str]


def case_values(size: str, names: list[str]) -> Values:
    """The values a case writes. In the 'mix' size every key keeps the length mix_value_len gives it."""
    if size != MIX:
        return 'v' * SIZES[size]
    by_len = {chars: 'v' * chars for chars, _ in VALUE_MIX}  # Four strings, shared by every key.
    return {name: by_len[mix_value_len(name)] for name in names}


def mix_summary(keys: int) -> dict[str, Any]:
    """For meta.value_mix: the distribution, and what a dataset of `keys` keys actually holds."""
    lens = [mix_value_len(name) for name in key_names(keys)]
    return {
        'percent_of_keys_by_chars': {str(chars): pct for chars, pct in VALUE_MIX},
        'mean_chars': SIZES[MIX],
        'picked_by': 'blake2b(key, digest_size=8, keyed) % 100 against the cumulative percentages',
        'dataset_keys': keys,
        'dataset_keys_by_chars': {str(chars): lens.count(chars) for chars, _ in VALUE_MIX},
        'dataset_mean_chars': statistics.fmean(lens) if lens else math.nan,
    }


def key_names(keys: int) -> list[str]:
    return [''.join(('t:', str(i))) for i in range(keys)]


def plan_len(keys: int) -> int:
    """Operations per plan: whole passes over the keys, at least two and at least PLAN_MIN operations."""
    return keys * max(2, math.ceil(PLAN_MIN / keys))


def key_picks(keys: int, dist: str, seed: int) -> list[int]:
    """Key indices for one plan. Uniform: shuffled passes over every key. Zipf (s=1.0): draws over every key."""
    rng = random.Random(seed)
    length = plan_len(keys)
    if dist == 'zipf':
        weights = list(itertools.accumulate(1 / (rank + 1) for rank in range(keys)))
        return rng.choices(range(keys), cum_weights=weights, k=length)
    picks: list[int] = []
    for _ in range(length // keys):
        one_pass = list(range(keys))
        rng.shuffle(one_pass)
        picks += one_pass
    return picks


def key_plan(names: list[str], dist: str, every: int, seed: int, tail: bool = False) -> Plan:
    """A cycling plan of (key, is_write, is_timed) triples, built per thread so nothing Python-level is shared.

    About one get in 61 is timed, and one operation in 100 when it's a write (every write in the 99:1
    mix, every 10th in 90:10, every 50th in 50:50), so the timing costs well under 1% of the loop. In
    a tail case (`tail`, values over 1 KB, where one operation takes microseconds) every write and one
    get in 7 are timed, so the window can gather TAIL_SAMPLES of each.
    """
    plan = []
    for i, k in enumerate(key_picks(len(names), dist, seed)):
        write = i % every == 0
        if tail:
            timed = write or i % TAIL_GET_SAMPLE_EVERY == 3
        else:
            timed = i % 100 == 0 if write else i % GET_SAMPLE_EVERY == 7
        plan.append((names[k], write, timed))
    return plan


def plan_summary(keys: int) -> dict[str, Any]:
    """For meta.plans: the plan length and thread 0's distinct keys per distribution."""
    return {
        'dataset_keys': keys,
        'plan_len': plan_len(keys),
        'distinct_keys': {dist: len(set(key_picks(keys, dist, 0))) for dist in DISTS},
        'measured_window_starts_at': plan_len(keys) // 2,
    }


def dataset_keys(size: str, keys: int, quick: bool) -> int:
    """1 KB values get `keys` keys; larger values (and the mix, by its mean) get a fixed byte budget, so filling
    stays quick and every dataset stays four times the largest page-cache budget compared (FanoutCache(8)'s 16 MiB)."""
    if size == DEFAULT_SIZE:
        return keys
    return (QUICK_SIZING_BYTES if quick else SIZING_BYTES) // SIZES[size]


def fill_marker(directory: Path | str) -> Path:
    directory = Path(directory)
    return directory.with_name(directory.name + '.filled')


def filled_keys(directory: Path | str) -> int:
    return json.loads(fill_marker(directory).read_text())['keys']


def ensure_filled(variant: str, directory: Path, keys: int, size: str) -> None:
    """Fill the variant's database once per run (in this process, which never measures memory)."""
    marker = fill_marker(directory)
    wanted_marker = {'keys': keys, 'size': size, 'value': SIZES[size]}
    if marker.exists() and json.loads(marker.read_text()) == wanted_marker:
        return
    started = time.perf_counter()
    directory.parent.mkdir(parents=True, exist_ok=True)
    names = key_names(keys)
    values = case_values(size, names)
    cache = make(variant, directory)
    for name in names:
        cache.set(name, values if isinstance(values, str) else values[name])
    cache.close()
    marker.write_text(json.dumps(wanted_marker))
    print(
        f'  filled {variant}: {keys:,} keys x {size_text(size)} in {time.perf_counter() - started:.1f} s',
        file=sys.stderr,
    )


def run_loop(cache: Any, plan: Plan, values: Values, stop: Callable[[], bool], samples: Samples, start: int = 0) -> int:
    """Run the plan in a cycle from operation `start` (rounded down to a chunk) until `stop()`; returns operations.

    A write sets `values` itself when it's one string, or the key's own value from the 'mix' size's dict.
    """
    get, set_ = cache.get, cache.set
    value = values if isinstance(values, str) else ''
    per_key = None if isinstance(values, str) else values
    clock = time.perf_counter_ns
    gets, sets = samples.gets, samples.sets
    cap = len(gets)
    n_gets = n_sets = 0
    samples.n_gets = samples.n_sets = 0
    ops = 0
    # Check the clock every STOP_CHECK operations, so a slow variant doesn't overrun its window by a whole plan.
    chunks = [plan[i : i + STOP_CHECK] for i in range(0, len(plan), STOP_CHECK)]
    first = (start // STOP_CHECK) % len(chunks)
    for chunk in itertools.cycle(chunks[first:] + chunks[:first]):
        if stop():
            break
        for key, write, timed in chunk:
            if timed:
                if write:
                    v = value if per_key is None else per_key[key]
                    t0 = clock()
                    set_(key, v)
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
                set_(key, value if per_key is None else per_key[key])
            else:
                get(key)
        ops += len(chunk)
        samples.n_gets, samples.n_sets = n_gets, n_sets
    return ops


def run_threads(
    cache: Any,
    plans: list[Plan],
    values: Values,
    seconds: float,
    samples: list[Samples],
    start: int = 0,
    min_samples: int = 0,
    max_seconds: float = 0.0,
) -> float:
    """ops/s over a window of `seconds`, or longer, up to `max_seconds`, until every kind has `min_samples`."""
    threads = len(plans)
    counts = [0] * threads
    stop = threading.Event()
    barrier = threading.Barrier(threads + 1)

    def worker(n: int) -> None:
        barrier.wait()
        counts[n] = run_loop(cache, plans[n], values, stop.is_set, samples[n], start)

    pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for t in pool:
        t.start()
    barrier.wait()
    begin = time.perf_counter()
    time.sleep(seconds)
    if min_samples:
        deadline = begin + max(seconds, max_seconds)
        while time.perf_counter() < deadline:
            taken = min(sum(s.n_gets for s in samples), sum(s.n_sets for s in samples))
            if taken >= min_samples:
                break
            time.sleep(0.05)
    stop.set()
    for t in pool:
        t.join()
    return sum(counts) / (time.perf_counter() - begin)


def is_tail(case: Case) -> bool:
    """The cases with values over 1 KB (the mix too), which time every write and run until they have TAIL_SAMPLES."""
    return case[4] != DEFAULT_SIZE


def thread_child(variant: str, directory: str, case: Case, timing: tuple[float, float], out: Any) -> None:
    """One threads case in a fresh process: baseline, open, warm, measure, report."""
    _, dist, mix, threads, size = case
    warm, seconds = timing
    tail = is_tail(case)
    import_variant(variant)
    names = key_names(filled_keys(directory))
    values = case_values(size, names)
    plans = [key_plan(names, dist, MIXES[mix], n, tail) for n in range(threads)]
    del names
    longest = max(seconds, TAIL_MAX_SECONDS) if tail else seconds
    samples = [Samples(longest) for _ in range(threads)]
    base = memory_baseline()
    cache = make(variant, Path(directory))
    run_threads(cache, plans, values, warm, samples)  # The warm-up's samples are overwritten.
    middle = len(plans[0]) // 2  # Measure from the plans' midpoint, not from where the warm-up started.
    if tail:
        rate = run_threads(cache, plans, values, seconds, samples, middle, TAIL_SAMPLES, TAIL_MAX_SECONDS)
    else:
        rate = run_threads(cache, plans, values, seconds, samples, middle)
    report = memory_report(base)
    cache.close()
    out.put({'ops': rate, **report, **latency_report(samples)})


def collect(out: Any, children: list[Any], count: int, timeout: float) -> list[Any]:
    """`count` results from the queue, failing fast if a child exits with an error rather than waiting out `timeout`."""
    deadline = time.monotonic() + timeout
    results: list[Any] = []
    while len(results) < count:
        try:
            results.append(out.get(timeout=0.5))
            continue
        except queue.Empty:
            pass
        failed = [p for p in children if p.exitcode not in (None, 0)]
        if failed:
            raise RuntimeError(f'case process {failed[0].pid} exited with code {failed[0].exitcode}')
        if time.monotonic() > deadline:
            raise TimeoutError(f'no result from the case processes after {timeout:.0f} s')
    return results


def stop_children(children: list[Any], grace: float) -> None:
    """Give the children `grace` seconds to exit, then terminate whatever is still running and reap everything."""
    deadline = time.monotonic() + grace
    for p in children:
        p.join(max(0.0, deadline - time.monotonic()))
    for p in children:
        if p.is_alive():
            p.terminate()
    for p in children:
        p.join(5)
        if p.is_alive():
            p.kill()
            p.join(5)


def threads_case(variant: str, directory: Path, case: Case, timing: tuple[float, float]) -> dict[str, Any]:
    ctx = multiprocessing.get_context('spawn')
    out = ctx.Queue()
    child = ctx.Process(target=thread_child, args=(variant, str(directory), case, timing, out))
    grace = 0.0  # A failure kills the child at once; a result gives it time to exit on its own.
    try:
        child.start()
        limit = 120 + sum(timing) + (TAIL_MAX_SECONDS if is_tail(case) else 0)
        result = collect(out, [child], 1, limit)[0]
        grace = 30.0
        return result
    finally:
        stop_children([child], grace)


def wait_for_start(start: Any, children: list[Any], timeout: float) -> None:
    """Join the start barrier, aborting it (which frees the children waiting on it) if a child dies first."""
    broken: list[BaseException] = []

    def waiter() -> None:
        try:
            start.wait(timeout=timeout)
        except threading.BrokenBarrierError as exc:
            broken.append(exc)

    thread = threading.Thread(target=waiter, daemon=True)
    thread.start()
    while thread.is_alive():
        failed = [p for p in children if p.exitcode not in (None, 0)]
        if failed:
            start.abort()
            thread.join()
            raise RuntimeError(f'case process {failed[0].pid} exited with code {failed[0].exitcode} before the start')
        thread.join(0.2)
    if broken:
        raise RuntimeError(f'the case processes never all reached the start within {timeout:.0f} s')


def proc_child(
    variant: str, directory: str, case: Case, seed: int, timing: tuple[float, float], start: Any, out: Any
) -> None:
    _, dist, mix, procs, size = case
    warm, seconds = timing
    tail = is_tail(case)
    import_variant(variant)
    names = key_names(filled_keys(directory))
    values = case_values(size, names)
    plan = key_plan(names, dist, MIXES[mix], seed, tail)
    del names
    samples = Samples(max(seconds, TAIL_MAX_SECONDS) if tail else seconds)
    base = memory_baseline()
    cache = make(variant, Path(directory))
    warm_end = time.perf_counter() + warm
    run_loop(cache, plan, values, lambda: time.perf_counter() >= warm_end, samples)
    start.wait(timeout=120)  # Every process has opened and warmed up before any clock starts.
    begin = time.perf_counter()
    end = begin + seconds
    # A tail case's process runs past `seconds` until it holds its share of TAIL_SAMPLES get and set samples, up
    # to TAIL_MAX_SECONDS, so the case's p99.9 rests on at least TAIL_SAMPLES. Its ops/s is over its own window.
    need = math.ceil(TAIL_SAMPLES / procs) if tail else 0
    deadline = begin + max(seconds, TAIL_MAX_SECONDS)

    def done() -> bool:
        now = time.perf_counter()
        if now < end:
            return False
        return min(samples.n_gets, samples.n_sets) >= need or now >= deadline

    # From the plan's midpoint, not from where the warm-up started.
    ops = run_loop(cache, plan, values, done, samples, len(plan) // 2)
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
    grace = 0.0
    try:
        for p in children:
            p.start()
        wait_for_start(start, children, 120)
        results = collect(out, children, procs, 120 + sum(timing) + (TAIL_MAX_SECONDS if is_tail(case) else 0))
        grace = 30.0
    finally:
        stop_children(children, grace)
    return {
        'ops': sum(r['ops'] for r in results),
        'rss_mib': statistics.median(r['rss_mib'] for r in results),
        'peak_mib': statistics.median(r['peak_mib'] for r in results),
        'private_mib': statistics.median(r['private_mib'] for r in results),
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


SMALL_CASES = ('small-1t', 'small-4t-shared', 'small-4t-separate')


def failed_entry(name: str, error: str) -> Any:
    """What a failed case records for its round: NaN everywhere, and the error."""
    if name.startswith('small'):
        return math.nan
    return {
        'ops': math.nan,
        'rss_mib': math.nan,
        'peak_mib': math.nan,
        'private_mib': math.nan,
        'get_us': percentiles([]),
        'set_us': percentiles([]),
        'error': error,
    }


def worker_main(args: argparse.Namespace) -> None:
    """Run one variant's cases for one round. A failed case is recorded and the rest still run; results are
    written after every case, so the driver keeps them if this process dies."""
    check_release_build()
    raise_fd_limit()
    timing = (0.1, 0.3) if args.quick else (WARM_SECONDS, args.seconds)
    job = json.loads(Path(args.job).read_text())
    results: dict[str, Any] = {}
    for raw in job['cases']:
        case: Case = (raw[0], raw[1], raw[2], int(raw[3]), raw[4])
        name = case_name(case)
        size = case[4]
        directory = Path(args.data_dir) / size
        try:
            ensure_filled(args.variant, directory, dataset_keys(size, args.keys, args.quick), size)
            run = threads_case if case[0] == 'threads' else procs_case
            results[name] = run(args.variant, directory, case, timing)
        except Exception as exc:
            traceback.print_exc()
            print(f'  {args.variant} {name} failed: {exc!r}', file=sys.stderr)
            results[name] = failed_entry(name, repr(exc))
        Path(args.out).write_text(json.dumps(results))
    if job['small']:
        try:
            with tempfile.TemporaryDirectory(prefix='dco-fanout-small-') as tmp_name:
                results.update(small_cases(args.variant, Path(tmp_name), lambda name: wanted(args, name)))
        except Exception as exc:
            traceback.print_exc()
            print(f'  {args.variant} small cases failed: {exc!r}', file=sys.stderr)
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


def expected_cases(args: argparse.Namespace, plan: dict[str, list[Case]], small: set[str], variant: str) -> list[str]:
    names = [case_name(c) for c in plan[variant]]
    return names + ([n for n in SMALL_CASES if wanted(args, n)] if variant in small else [])


def summarize(samples: dict[str, dict[str, list[Any]]]) -> dict[str, dict[str, dict[str, Any]]]:
    summary: dict[str, dict[str, dict[str, Any]]] = {}
    for variant, by_case in samples.items():
        summary[variant] = {}
        for case, values in by_case.items():
            if isinstance(values[0], dict):
                entry = spread([v['ops'] for v in values])
                entry['mix'] = mix_of(case)
                entry['value_bytes'] = SIZES[size_of(case)]  # The mean for the 'mix' size (meta.value_mix).
                entry['value_size'] = size_of(case)
                entry['rss_growth_mib'] = spread([v['rss_mib'] for v in values])
                entry['peak_growth_mib'] = spread([v['peak_mib'] for v in values])
                entry['private_growth_mib'] = spread([v.get('private_mib', math.nan) for v in values])
                entry['latency_us'] = {
                    op: {q: spread([v[f'{op}_us'][q] for v in values]) for q in ('p50', 'p99', 'p999')}
                    | {'n': [v[f'{op}_us']['n'] for v in values]}
                    for op in ('get', 'set')
                }
                errors = [v.get('error') for v in values]
                if any(errors):
                    entry['errors'] = errors
            else:
                entry = spread(values)
            summary[variant][case] = entry
    return summary


def driver_main(args: argparse.Namespace) -> None:
    check_release_build()
    import diskcache
    import diskcache_oxide

    nofile = raise_fd_limit()  # Before any worker starts: every worker and case process inherits it.
    warn_low_fd_limit(nofile)
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
    errors: list[dict[str, Any]] = []
    run_started = time.perf_counter()
    groups = {
        name: {
            'about': GROUPS[name].about,
            'variants': group_variants(args, GROUPS[name]),
            'cases': [case_name(c) for c in group_cases(args, GROUPS[name])]
            + ([n for n in SMALL_CASES if wanted(args, n)] if GROUPS[name].small and small else []),
        }
        for name in selected_groups(args)
    }
    sizes_used = sorted({c[4] for cases in plan.values() for c in cases}, key=list(SIZES).index)
    plans = {s: plan_summary(dataset_keys(s, args.keys, args.quick)) for s in sizes_used}
    out_path = args.out or RESULTS_DIR / f'fanout-{python}-{args.machine}{"-quick" if args.quick else ""}.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)

    def write_results(rounds_done: int) -> dict[str, dict[str, dict[str, Any]]]:
        summary = summarize(samples)
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
            'rounds_done': rounds_done,
            'seconds': 0.3 if args.quick else args.seconds,
            'warm_seconds': 0.1 if args.quick else WARM_SECONDS,
            'tail_sampling': {
                'cases': 'value sizes over 1 KB',
                'timed': f'every write and one get in {TAIL_GET_SAMPLE_EVERY}',
                'window': f'at least seconds, then until {TAIL_SAMPLES:,} get and set samples, '
                f'at most {TAIL_MAX_SECONDS:g} s',
            },
            'percentile_min_samples': {'p99': MIN_P99, 'p999': MIN_P999},
            'quick': args.quick,
            'full': args.full,
            'only': args.only,
            'keys': args.keys,
            'value_bytes': SIZES[DEFAULT_SIZE],
            'sizing_keys': {s: dataset_keys(s, args.keys, args.quick) for s in SIZES if s != DEFAULT_SIZE},
            'value_mix': mix_summary(dataset_keys(MIX, args.keys, args.quick)),
            'plans': plans,
            'mix': f'1 write in {MIXES[HEADLINE]} operations',
            'mixes': {mix: f'1 write in {every} operations' for mix, every in MIXES.items()},
            'headline_mix': HEADLINE,
            'groups': groups,
            'variants': {v: VARIANTS[v][1] + repr(VARIANTS[v][2]) for v in variants},
            'memory': 'MiB over the post-import baseline of each case process, measured just after the window: '
            "rss_growth = current RSS (includes resident pages of mapped files, such as stock diskcache's "
            '64 MiB database mmap), peak_growth = ru_maxrss, private_growth = process-private memory '
            '(macOS phys_footprint, Linux Private_Dirty + Swap; excludes mapped files); procs cases report '
            'the median process',
            'nofile': nofile,
            'errors': errors,
            'loads': loads,
            'elapsed_minutes': (time.perf_counter() - run_started) / 60,
            'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        }
        out_path.write_text(json.dumps({'meta': meta, 'results': summary}, indent=2) + '\n')
        return summary

    summary: dict[str, dict[str, dict[str, Any]]] = {}
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
                worker_error: Optional[str] = None
                try:
                    subprocess.run(cmd, check=True)
                except subprocess.CalledProcessError as exc:
                    worker_error = f'worker exited with code {exc.returncode}'
                    errors.append({'round': r + 1, 'variant': variant, 'case': None, 'error': worker_error})
                    print(f'round {r + 1}: {variant} {worker_error}; its missing cases are NaN', file=sys.stderr)
                got = json.loads(out.read_text()) if out.exists() else {}
                for case in expected_cases(args, plan, small, variant):
                    value = got.get(case)
                    if value is None:
                        value = failed_entry(case, worker_error or 'no result')
                    elif isinstance(value, dict) and value.get('error'):
                        errors.append({'round': r + 1, 'variant': variant, 'case': case, 'error': value['error']})
                    samples[variant].setdefault(case, []).append(value)
                minutes = (time.perf_counter() - run_started) / 60
                print(
                    f'round {r + 1}/{args.rounds}: {variant} done (load {loadavg():.1f}, {minutes:.1f} min)',
                    file=sys.stderr,
                )
            loads[-1]['load_after'] = loadavg()
            summary = write_results(r + 1)  # After every round, so a run that dies later keeps what it measured.

    print(f'\nPython {python}, {args.keys:,} keys x 1 KB, medians over {args.rounds} round(s)')
    for name, info in groups.items():
        (print_detail if GROUPS[name].detail else print_tables)(name, info, summary)
    if errors:
        print(f'\n{len(errors)} failure(s), recorded as NaN:', file=sys.stderr)
        for e in errors:
            print(f'  round {e["round"]} {e["variant"]} {e["case"] or "(worker)"}: {e["error"]}', file=sys.stderr)
    elapsed = (time.perf_counter() - run_started) / 60
    print(f'\nWrote {out_path} ({elapsed:.1f} min)')


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
                print(f'-- {key[1]} ({label}), {size_text(key[0])}')
            row = f'{case:30}'
            for v in variants:
                entry = summary.get(v, {}).get(case)
                row += f'{cell(entry) if entry else "-":>{width}}'
            print(row)

    section('Throughput, ops/s (spread = (max - min) / median across rounds)', ops_cell, cases)
    memory_rows = [c for c in cases if not c.startswith('small')]
    if memory_rows:
        section(
            'Memory growth per process in MiB: RSS after the window / RSS peak / private after the window '
            '(private excludes mapped files; compare that one; procs: the median process)',
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
    print(
        'kops/s: median, min-max over rounds, cv = stdev/mean; memory growth MiB: RSS / private (excludes '
        'mapped files); latency us, median over rounds, - = too few samples; n = fewest samples in a round'
    )
    header = (
        f'  {"variant":21}{"kops/s":>8}{"min-max":>14}{"cv":>6}{"rss/priv":>12}'
        f'{"get p50":>9}{"p99":>8}{"p99.9":>8}{"set p50":>9}{"p99":>8}{"p99.9":>8}{"n get/set":>14}  flags'
    )
    for case in sorted((c for c in info['cases'] if not c.startswith('small')), key=case_order):
        rows = [(v, summary[v][case]) for v in variants if case in summary.get(v, {})]
        if not rows:
            continue
        mix, size = mix_of(case), size_of(case)
        label = 'headline' if mix == HEADLINE else 'stress case, write-heavy'
        print(f'\n{case}  ({mix} {label}, {size_text(size)})\n{header}')

        def middle(pick: Callable[[dict[str, Any]], float], rows: list[tuple[str, dict[str, Any]]] = rows) -> float:
            values = [x for x in (pick(e) for _, e in rows) if not math.isnan(x)]
            return statistics.median(values) if values else math.nan

        mid_cv = middle(lambda e: e['cv'])
        mid_get = middle(lambda e: e['latency_us']['get']['p999']['median'])
        mid_set = middle(lambda e: e['latency_us']['set']['p999']['median'])
        for variant, e in rows:
            lat = {op: {q: e['latency_us'][op][q]['median'] for q in ('p50', 'p99', 'p999')} for op in ('get', 'set')}
            flags = ['FAILED'] if e.get('errors') else []
            if e['cv'] > 0.05 and e['cv'] > 2 * mid_cv:
                flags.append('UNSTABLE')
            if lat['get']['p999'] > 3 * mid_get:
                flags.append('TAIL-GET')
            if lat['set']['p999'] > 3 * mid_set:
                flags.append('TAIL-SET')
            spread_text = f'{e["min"] / 1000:,.0f}-{e["max"] / 1000:,.0f}'
            mem = f'{e["rss_growth_mib"]["median"]:.1f}/{e["private_growth_mib"]["median"]:.1f}'
            fewest = f'{min_count(e["latency_us"]["get"]["n"])}/{min_count(e["latency_us"]["set"]["n"])}'
            print(
                f'  {variant:21}{e["median"] / 1000:>8,.0f}{spread_text:>14}{e["cv"] * 100:>5.1f}%{mem:>12}'
                f'{us(lat["get"]["p50"]):>9}{us(lat["get"]["p99"]):>8}{us(lat["get"]["p999"]):>8}'
                f'{us(lat["set"]["p50"]):>9}{us(lat["set"]["p99"]):>8}{us(lat["set"]["p999"]):>8}{fewest:>14}'
                f'  {" ".join(flags)}'
            )


def min_count(counts: list[int]) -> str:
    """The fewest samples any round took (0 for a failed round is left out, so a failure doesn't hide the rest)."""
    real = [n for n in counts if n]
    return f'{min(real):,}' if real else '0'


def ops_cell(entry: dict[str, Any]) -> str:
    median = entry['median']
    if math.isnan(median):
        return 'FAILED'
    pct = (entry['max'] - entry['min']) / median * 100 if median else 0.0
    return f'{median / 1000:,.0f}k {pct:2.0f}%'


def memory_cell(entry: dict[str, Any]) -> str:
    if 'rss_growth_mib' not in entry:
        return '-'
    rss, peak = entry['rss_growth_mib']['median'], entry['peak_growth_mib']['median']
    return f'{rss:.1f}/{peak:.1f}/{entry["private_growth_mib"]["median"]:.1f}'


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
    parser.add_argument('--sizes', default=','.join(SIZES), help=f'Only these value sizes: {", ".join(SIZES)}.')
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
    if args.quick and args.keys == KEYS:
        args.keys = QUICK_KEYS  # The driver passes it on, so the workers fill and read this many too.
    check_grid_args(args)
    if args.worker:
        worker_main(args)
    else:
        driver_main(args)


if __name__ == '__main__':
    main()
