"""FanoutCache benchmark (plan 13, step 13i): does sharding remove the per-file read-scaling ceiling?

Usage (from the repo root, on AC power, on a quiet machine):
    venv/bin/python -m bench.fanout              # 3.14, 3 interleaved rounds
    .venv-ft/bin/python -m bench.fanout          # 3.14t
    venv/bin/python -m bench.fanout --quick      # smoke test
    venv/bin/python -m bench.fanout --only small --rounds 5

Variants (the plan's list): oxide Cache, oxide FanoutCache(8) at the default connection budget and
at max_connections=16, stock diskcache Cache and stock diskcache FanoutCache(8). Oxide Cache with
max_connections=16 is added so the budget isn't confounded with the shard count.

Cases:
- threads-{uniform,zipf}-N: N threads in one process sharing one cache, a 99:1 get:set mix over
  1,000 keys with ~1 KB values (uniform keys, or Zipf s=1.0), N = 1, 2, 4, 8. ops/s.
- procs-{uniform,zipf}-N: the same mix from N spawned processes, each opening the directory itself.
- small-1t / small-4t-shared / small-4t-separate: scale_probe's free-threaded small-read case (700
  URL-shaped keys, 6-byte values, reads only, 4 threads) on one shared handle, or on four separate
  instances of the variant (one directory each). This is what FanoutCache exists for.

Each round runs every variant in a fresh process (the order rotates each round, so drift hits all of
them alike). The driver reports the median over rounds and the min-max spread. Only compare results
from the same machine. Writes bench/results/fanout-<python>-<machine>.json.
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
from collections.abc import Callable
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).parent / 'results'
THREAD_COUNTS = (1, 2, 4, 8)
KEYS = 1_000
EVERY = 100  # One write in every 100 operations: the 99:1 mix.
VALUE = 'v' * 1_024
PLAN_LEN = 10_000
TIMEOUT = 10.0

SMALL_KEYS = 700
SMALL_OPS = 60_000  # Reads per thread, as in bench/scaling/scale_probe.py.

# variant -> (module, class, constructor kwargs). The shard count is part of the name.
VARIANTS: dict[str, tuple[str, str, dict[str, Any]]] = {
    'oxide-cache': ('diskcache_oxide', 'Cache', {}),
    'oxide-cache-c16': ('diskcache_oxide', 'Cache', {'max_connections': 16}),
    'oxide-fanout8': ('diskcache_oxide', 'FanoutCache', {'shards': 8}),
    'oxide-fanout8-c16': ('diskcache_oxide', 'FanoutCache', {'shards': 8, 'max_connections': 16}),
    # Not in the default run (see DEFAULT_VARIANTS): wider budgets, to tell a connection-budget limit from a shard one.
    'oxide-cache-c32': ('diskcache_oxide', 'Cache', {'max_connections': 32}),
    'oxide-fanout8-c32': ('diskcache_oxide', 'FanoutCache', {'shards': 8, 'max_connections': 32}),
    'stock-cache': ('diskcache', 'Cache', {}),
    'stock-fanout8': ('diskcache', 'FanoutCache', {'shards': 8}),
}
DEFAULT_VARIANTS = [v for v in VARIANTS if not v.endswith('-c32')]


def make(variant: str, directory: Path) -> Any:
    import importlib

    module, cls, kwargs = VARIANTS[variant]
    return getattr(importlib.import_module(module), cls)(str(directory), timeout=TIMEOUT, **kwargs)


def check_release_build() -> None:
    import diskcache_oxide

    flag = getattr(diskcache_oxide, '_DEBUG_BUILD', None)
    if flag is None:
        flag = getattr(getattr(diskcache_oxide, '_core', None), '_DEBUG_BUILD', False)
    if flag:
        raise SystemExit('refusing to benchmark a debug build of diskcache_oxide; rebuild in release mode')


# ---------------------------------------------------------------------------------- the 99:1 mix


def key_plan(dist: str, seed: int) -> list[tuple[str, bool]]:
    """A cycling plan of (key, is_write) pairs, built per thread so nothing Python-level is shared."""
    rng = random.Random(seed)
    names = [''.join(('t:', str(i))) for i in range(KEYS)]
    if dist == 'zipf':
        weights = list(itertools.accumulate(1 / (rank + 1) for rank in range(KEYS)))
        picks = rng.choices(range(KEYS), cum_weights=weights, k=PLAN_LEN)
    else:
        picks = [rng.randrange(KEYS) for _ in range(PLAN_LEN)]
    return [(names[k], i % EVERY == 0) for i, k in enumerate(picks)]


def fill(cache: Any) -> None:
    for i in range(KEYS):
        cache.set(f't:{i}', VALUE)


def run_loop(cache: Any, plan: list[tuple[str, bool]], stop: Callable[[], bool]) -> int:
    get, set_ = cache.get, cache.set
    ops = 0
    while not stop():
        for key, write in plan:
            if write:
                set_(key, VALUE)
            else:
                get(key)
        ops += len(plan)
    return ops


def threads_case(cache: Any, dist: str, threads: int, seconds: float) -> float:
    counts = [0] * threads
    stop = threading.Event()
    barrier = threading.Barrier(threads + 1)

    def worker(n: int) -> None:
        plan = key_plan(dist, n)
        barrier.wait()
        counts[n] = run_loop(cache, plan, stop.is_set)

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


def proc_main(variant: str, directory: str, dist: str, seed: int, start_at: float, seconds: float, out: Any) -> None:
    cache = make(variant, Path(directory))
    plan = key_plan(dist, seed)
    cache.get('t:0')  # Open this process's first connection before the clock starts.
    while time.time() < start_at:
        time.sleep(0.001)
    begin = time.perf_counter()
    end = begin + seconds
    ops = run_loop(cache, plan, lambda: time.perf_counter() >= end)
    elapsed = time.perf_counter() - begin
    cache.close()
    out.put(ops / elapsed)


def procs_case(variant: str, directory: Path, dist: str, procs: int, seconds: float) -> float:
    ctx = multiprocessing.get_context('spawn')
    out = ctx.Queue()
    start_at = time.time() + 1.5 + 0.15 * procs
    children = [
        ctx.Process(target=proc_main, args=(variant, str(directory), dist, 100 + n, start_at, seconds, out))
        for n in range(procs)
    ]
    for p in children:
        p.start()
    rates = [out.get(timeout=120) for _ in children]
    for p in children:
        p.join()
    return sum(rates)


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


# ---------------------------------------------------------------------------------- worker


def worker_main(args: argparse.Namespace) -> None:
    check_release_build()
    seconds = 0.5 if args.quick else args.seconds
    counts = (1, 4) if args.quick else THREAD_COUNTS

    def wanted(name: str) -> bool:
        return not args.only or any(name.startswith(prefix) for prefix in args.only)

    results: dict[str, float] = {}
    with tempfile.TemporaryDirectory(prefix='dco-fanout-') as tmp_name:
        tmp = Path(tmp_name)
        for dist in ('uniform', 'zipf'):
            if not (wanted(f'threads-{dist}') or wanted(f'procs-{dist}')):
                continue
            directory = tmp / dist
            cache = make(args.variant, directory)
            fill(cache)
            if wanted(f'threads-{dist}'):
                threads_case(cache, dist, 4, 0.3)  # Warm the connections and the page cache.
                for n in counts:
                    results[f'threads-{dist}-{n}'] = threads_case(cache, dist, n, seconds)
            cache.close()
            if wanted(f'procs-{dist}'):
                for n in counts:
                    results[f'procs-{dist}-{n}'] = procs_case(args.variant, directory, dist, n, seconds + 0.5)
        if wanted('small'):
            results.update(small_cases(args.variant, tmp, wanted))
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


def driver_main(args: argparse.Namespace) -> None:
    check_release_build()
    import diskcache
    import diskcache_oxide

    variants = args.variant_list or DEFAULT_VARIANTS
    gil_disabled = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
    python = platform.python_version() + ('t' if gil_disabled else '')
    samples: dict[str, dict[str, list[float]]] = {v: {} for v in variants}
    loads: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix='dco-fanout-run-') as tmp:
        for r in range(args.rounds):
            order = variants[r % len(variants) :] + variants[: r % len(variants)]
            loads.append({'round': r + 1, 'load_before': loadavg(), 'busy': busy_processes()})
            for variant in order:
                out = Path(tmp) / f'{variant}-{r}.json'
                cmd = [sys.executable, '-m', 'bench.fanout', '--worker', '--variant', variant, '--out', str(out)]
                cmd += ['--seconds', str(args.seconds)] + (['--quick'] if args.quick else [])
                for prefix in args.only or []:
                    cmd += ['--only', prefix]
                subprocess.run(cmd, check=True)
                for case, value in json.loads(out.read_text()).items():
                    samples[variant].setdefault(case, []).append(value)
                print(f'round {r + 1}/{args.rounds}: {variant} done (load {loadavg():.1f})', file=sys.stderr)
            loads[-1]['load_after'] = loadavg()

    summary = {
        variant: {
            case: {
                'median': statistics.median(values),
                'min': min(values),
                'max': max(values),
                'samples': values,
            }
            for case, values in cases.items()
        }
        for variant, cases in samples.items()
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
        'seconds': args.seconds,
        'quick': args.quick,
        'only': args.only,
        'keys': KEYS,
        'mix': f'1 write in {EVERY} operations',
        'loads': loads,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    out_path = args.out or RESULTS_DIR / f'fanout-{python}-{args.machine}{"-quick" if args.quick else ""}.json'
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({'meta': meta, 'results': summary}, indent=2) + '\n')
    print_table(summary, variants)
    print(f'Wrote {out_path}')


def print_table(summary: dict[str, dict[str, dict[str, Any]]], variants: list[str]) -> None:
    cases = sorted({c for v in summary.values() for c in v}, key=lambda c: (c.rsplit('-', 1)[0], c))
    print(f'\n{"case":24}' + ''.join(f'{v:>20}' for v in variants))
    for case in cases:
        row = f'{case:24}'
        for v in variants:
            cell = summary[v].get(case)
            row += f'{cell["median"]:>20,.0f}' if cell else f'{"-":>20}'
        print(row)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--seconds', type=float, default=1.5, help='Measured seconds per threaded or process case.')
    parser.add_argument('--quick', action='store_true', help='Smaller run for smoke-testing the script.')
    parser.add_argument('--machine', default=platform.node().split('.')[0] or 'unknown')
    parser.add_argument('--out', type=Path, help='Override the output path.')
    parser.add_argument(
        '--only',
        action='append',
        metavar='PREFIX',
        help='Only cases starting with this prefix (repeatable): threads, procs, threads-zipf, small, ...',
    )
    parser.add_argument(
        '--variant-list', nargs='+', choices=sorted(VARIANTS), help='Variants to run (default: all, interleaved).'
    )
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--variant', choices=sorted(VARIANTS), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker:
        worker_main(args)
    else:
        driver_main(args)


if __name__ == '__main__':
    main()
