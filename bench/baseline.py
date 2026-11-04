"""Baseline latency and throughput benchmarks, shaped like Talk Python's cache workloads.

Usage (from the repo root):
    venv/bin/python -m bench.baseline --backend diskcache
    .venv-ft/bin/python -m bench.baseline --backend diskcache --quick

Writes bench/results/<backend>-<python>-<machine>.json. Only compare results from the same machine.

Method:
- p50 latency is timed in batches (BATCH ops between two perf_counter_ns calls), because one timer
  call costs ~50-100 ns against a ~3 us operation. p99 needs per-call samples, which include
  that timer overhead.
- The GC is disabled while timing. Each workload runs TRIALS times and the median trial is reported.
"""

from __future__ import annotations

import argparse
import gc
import json
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

from tests.conformance.backends import BACKENDS, Backend
from tests.proctitle import set_title

RESULTS_DIR = Path(__file__).parent / 'results'
TRIALS = 5
BATCH = 1_000


def page_entry(size: int = 450 * 1024) -> dict[str, Any]:
    """The shape Talk Python's rendered_cache stores for show pages and RSS feeds."""
    body = random.Random(42).randbytes(size)  # Incompressible, so the stored size is ~size.
    return {
        'content_gz': body,
        'created': '2026-10-27T12:00:00+00:00',
        'etag': 'abc123',
        'version': 2,
    }


def time_batched(op: Callable[[int], Any], n_batches: int) -> float:
    """Median per-op nanoseconds over `n_batches` batches of BATCH calls."""
    per_op: list[float] = []
    i = 0
    for _ in range(n_batches):
        start = time.perf_counter_ns()
        for _ in range(BATCH):
            op(i)
            i += 1
        per_op.append((time.perf_counter_ns() - start) / BATCH)
    return statistics.median(per_op)


def time_per_call(op: Callable[[int], Any], n: int) -> list[int]:
    samples = []
    for i in range(n):
        start = time.perf_counter_ns()
        op(i)
        samples.append(time.perf_counter_ns() - start)
    return samples


def percentile(samples: list[int], pct: float) -> float:
    ordered = sorted(samples)
    return float(ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))])


def loop_overhead_ns() -> float:
    """Per-op cost of the batched harness itself (the lambda call and loop), subtracted from batched p50s."""
    gc.disable()
    try:
        return statistics.median(time_batched(lambda i: None, 50) for _ in range(TRIALS))
    finally:
        gc.enable()


def measure(op: Callable[[int], Any], *, n_batches: int, n_samples: int, warmup: int) -> dict[str, float]:
    """Median-of-TRIALS p50 (batched, harness overhead subtracted) and p99 (per-call, timer overhead included), in
    microseconds."""
    for i in range(warmup):
        op(i)

    p50s, p99s = [], []
    gc.disable()
    try:
        for _ in range(TRIALS):
            if n_batches:
                p50s.append(max(0.0, time_batched(op, n_batches) - LOOP_OVERHEAD_NS))
            samples = time_per_call(op, n_samples)
            p99s.append(percentile(samples, 99))
            if not n_batches:
                p50s.append(percentile(samples, 50))
    finally:
        gc.enable()

    return {
        'p50_us': statistics.median(p50s) / 1_000,
        'p99_us': statistics.median(p99s) / 1_000,
    }


def measure_once(op: Callable[[], Any]) -> dict[str, float]:
    """Median wall time of TRIALS single runs, in milliseconds (for scans)."""
    times = []
    gc.disable()
    try:
        for _ in range(TRIALS):
            start = time.perf_counter_ns()
            op()
            times.append(time.perf_counter_ns() - start)
    finally:
        gc.enable()
    return {'median_ms': statistics.median(times) / 1e6, 'min_ms': min(times) / 1e6}


def populate(cache: Any, count: int) -> None:
    """Rate-limit-shaped keys: 'h:<bucket>:<ip>' plus a sprinkle of other prefixes."""
    rng = random.Random(7)
    transact = getattr(cache, 'transact', None)
    chunk = 10_000
    for start in range(0, count, chunk):

        def write_chunk() -> None:
            for i in range(start, min(start + chunk, count)):
                prefix = 'h' if i % 4 else 'd'
                ip = f'{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(256)}.{i % 256}'
                cache.set(f'{prefix}:{i % 24}:{ip}:{i}', i, expire=3600)

        if transact is not None:
            with transact():
                write_chunk()
        else:
            write_chunk()


# get_random_wide: uniform random reads over a database far larger than any page cache, the case
# where the per-connection page cache (sqlite_cache_size) matters most. Hot and skewed reads don't
# depend on it.
WIDE_KEYS = 100_000


def random_wide_gets(
    backend: Backend,
    directory: Path,
    opts: dict[str, Any],
    n_batches: int,
    n_samples: int,
) -> dict[str, float]:
    cache = backend.make_cache(directory, timeout=10.0, size_limit=2**34, **opts)
    transact = getattr(cache, 'transact', None)
    body = 'x' * 1_000
    for start in range(0, WIDE_KEYS, 10_000):

        def write_chunk() -> None:
            for i in range(start, start + 10_000):
                cache.set(f'w:{i}', {'id': i, 'body': body})

        if transact is not None:
            with transact():
                write_chunk()
        else:
            write_chunk()
    rng = random.Random(11)
    keys = [f'w:{rng.randrange(WIDE_KEYS)}' for _ in range(65_536)]
    result = measure(
        lambda i: cache.get(keys[i & 65_535]),
        n_batches=n_batches // 4 or 1,
        n_samples=n_samples,
        warmup=5_000,
    )
    cache.close()
    return result


LOOP_OVERHEAD_NS = 0.0


def mcp_result() -> dict[str, Any]:
    """A small structured value, shaped like the MCP results Talk Python caches (a few hundred bytes)."""
    return {
        'tool': 'search_episodes',
        'ok': True,
        'count': 3,
        'results': [
            {
                'id': 500 + n,
                'title': f'Episode {500 + n}: Python news',
                'score': 0.9 - n / 10,
            }
            for n in range(3)
        ],
    }


WORKLOADS = (
    'get_hit_small',
    'get_hit_small_dict',
    'get_miss',
    'set_small',
    'get_random_wide',
    'get_page_entry_450k',
    'set_page_entry_450k',
    'add_incr_counter',
    'threads_99to1',
    'threads_1000to1',
    'scans',
)


def run(
    backend: Backend,
    quick: bool,
    only: set[str] | None = None,
    opts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the selected workloads (all by default); `opts` are passed to every make_cache call."""
    global LOOP_OVERHEAD_NS
    LOOP_OVERHEAD_NS = loop_overhead_ns()
    results: dict[str, Any] = {'harness_loop_overhead_ns': LOOP_OVERHEAD_NS}
    n_batches = 20 if quick else 100
    n_samples = 2_000 if quick else 10_000
    scan_sizes = [100_000] if quick else [100_000, 400_000]
    opts = opts or {}

    def want(name: str) -> bool:
        return only is None or name in only

    with tempfile.TemporaryDirectory(prefix='dco-bench-') as tmp:
        cache = backend.make_cache(Path(tmp) / 'latency', timeout=1.0, size_limit=2**30, **opts)

        small = 'x' * 100
        cache.set('small', small)
        if want('get_hit_small'):
            results['get_hit_small'] = measure(
                lambda i: cache.get('small'),
                n_batches=n_batches,
                n_samples=n_samples,
                warmup=5_000,
            )
        if want('get_hit_small_dict'):
            cache.set('small_dict', mcp_result())
            results['get_hit_small_dict'] = measure(
                lambda i: cache.get('small_dict'),
                n_batches=n_batches,
                n_samples=n_samples,
                warmup=5_000,
            )
        if want('get_miss'):
            results['get_miss'] = measure(
                lambda i: cache.get('missing'),
                n_batches=n_batches,
                n_samples=n_samples,
                warmup=5_000,
            )
        if want('set_small'):
            results['set_small'] = measure(
                lambda i: cache.set(f'small:{i % 1_000}', small),
                n_batches=n_batches // 4 or 1,
                n_samples=n_samples // 4,
                warmup=1_000,
            )

        if want('get_random_wide'):
            results['get_random_wide'] = random_wide_gets(backend, Path(tmp) / 'wide', opts, n_batches, n_samples)

        if want('get_page_entry_450k') or want('set_page_entry_450k'):
            entry = page_entry()
            cache.set('page', entry, expire=3600)
            if want('get_page_entry_450k'):
                results['get_page_entry_450k'] = measure(
                    lambda i: cache.get('page'),
                    n_batches=0,
                    n_samples=200 if quick else 1_000,
                    warmup=50,
                )
            if want('set_page_entry_450k'):
                results['set_page_entry_450k'] = measure(
                    lambda i: cache.set(f'page:{i % 50}', entry, expire=3600),
                    n_batches=0,
                    n_samples=200 if quick else 1_000,
                    warmup=50,
                )

        def rate_limit_bump(i: int) -> None:
            key = f'h:{i % 1_000}:10.0.0.{i % 256}'
            cache.add(key, 0, expire=3600)
            cache.incr(key)

        if want('add_incr_counter'):
            results['add_incr_counter'] = measure(
                rate_limit_bump,
                n_batches=n_batches // 4 or 1,
                n_samples=n_samples // 4,
                warmup=1_000,
            )
        for name, every in MIXES.items():
            if want(name):
                results[name] = threaded_throughput(cache, seconds=0.5 if quick else 1.5, every=every)
        cache.close()

        for size in scan_sizes if want('scans') else []:
            scan_cache = backend.make_cache(Path(tmp) / f'scan-{size}', timeout=10.0, size_limit=2**32)
            populate(scan_cache, size)
            label = f'{size // 1_000}k'
            results[f'iterkeys_prefix_scan_{label}'] = measure_once(
                lambda: [k for k in scan_cache.iterkeys() if k.startswith('d:')]
            )
            results[f'len_{label}'] = measure_once(lambda: len(scan_cache))
            results[f'volume_{label}'] = measure_once(lambda: scan_cache.volume())
            scan_cache.close()

    return results


THREAD_COUNTS = (1, 2, 4, 8)
# Read/write mixes for the threaded workloads: the 99:1 headline and the 1,000:1 Talk Python profile.
MIXES = {'threads_99to1': 100, 'threads_1000to1': 1_000}


def threaded_throughput(cache: Any, *, seconds: float, every: int) -> dict[str, float]:
    """ops/s at 1, 2, 4 and 8 threads sharing one cache, one ~1 KB write in every `every` operations.

    A share-nothing loop: each thread builds its own key strings and its operation plan, binds the
    methods once and checks the stop flag only between rounds. On free-threaded builds, a loop that
    shares Python objects across threads is contention-bound itself and measures the harness, not
    the cache.
    """
    value = 'v' * 1_024
    for i in range(1_000):
        cache.set(f't:{i}', value)

    results: dict[str, float] = {}
    for threads in THREAD_COUNTS:
        counts = [0] * threads
        stop = threading.Event()
        barrier = threading.Barrier(threads + 1)

        def worker(n: int) -> None:
            keys = [''.join(('t:', str(i))) for i in range(1_000)]
            get, set_ = cache.get, cache.set
            rng = random.Random(n)
            plan = [(keys[rng.randrange(1_000)], i % every == 0) for i in range(every * max(1, 1_000 // every))]
            barrier.wait()
            ops = 0
            while not stop.is_set():
                for key, write in plan:
                    if write:
                        set_(key, value)
                    else:
                        get(key)
                ops += len(plan)
            counts[n] = ops

        pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
        for t in pool:
            t.start()
        barrier.wait()
        start = time.perf_counter()
        time.sleep(seconds)
        stop.set()
        for t in pool:
            t.join()
        results[str(threads)] = sum(counts) / (time.perf_counter() - start)
    return results


def power_source() -> str:
    if sys.platform != 'darwin':
        return 'unknown'
    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True, text=True, timeout=5).stdout
    except OSError, subprocess.SubprocessError:
        return 'unknown'
    return 'ac' if 'AC Power' in out else 'battery' if 'Battery Power' in out else 'unknown'


def metadata(backend: Backend, machine: str, quick: bool) -> dict[str, Any]:
    gil_disabled = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
    return {
        'backend': backend.name,
        'backend_version': backend._module.__version__ if hasattr(backend._module, '__version__') else None,
        'python': platform.python_version() + ('t' if gil_disabled else ''),
        'gil_enabled_at_runtime': sys._is_gil_enabled() if hasattr(sys, '_is_gil_enabled') else True,
        'machine': machine,
        'platform': platform.platform(),
        'cpu': platform.processor() or platform.machine(),
        'cpu_count': os.cpu_count(),
        'power': power_source(),
        'quick': quick,
        'trials': TRIALS,
        'batch': BATCH,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--backend', default='diskcache', choices=sorted(BACKENDS))
    parser.add_argument(
        '--machine',
        default=platform.node().split('.')[0] or 'unknown',
        help='Label for the results file.',
    )
    parser.add_argument('--quick', action='store_true', help='Smaller run for smoke-testing the script.')
    parser.add_argument('--out', type=Path, help='Override the output path.')
    parser.add_argument(
        '--only',
        action='append',
        choices=WORKLOADS,
        help='Run only these workloads (repeatable).',
    )
    parser.add_argument(
        '--opt',
        action='append',
        default=[],
        metavar='NAME=VALUE',
        help='Extra cache option, e.g. spike_detach=true.',
    )
    args = parser.parse_args()

    set_title(f'bench[{args.backend}]')
    backend = BACKENDS[args.backend]()
    if getattr(getattr(backend, '_module', None), '_DEBUG_BUILD', False):
        raise SystemExit('refusing to benchmark a debug build of diskcache_oxide; rebuild in release mode')
    opts = {name: json.loads(value) for name, _, value in (o.partition('=') for o in args.opt)}
    meta = metadata(backend, args.machine, args.quick)
    meta['only'] = sorted(args.only) if args.only else None
    meta['opts'] = opts
    results = run(backend, args.quick, set(args.only) if args.only else None, opts)

    out = (
        args.out
        or RESULTS_DIR / f'{meta["backend"]}-{meta["python"]}-{meta["machine"]}{"-quick" if args.quick else ""}.json'
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'meta': meta, 'results': results}, indent=2) + '\n')
    print(json.dumps(results, indent=2))
    print(f'Wrote {out}')


if __name__ == '__main__':
    main()
