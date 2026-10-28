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
import gzip
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

RESULTS_DIR = Path(__file__).parent / 'results'
TRIALS = 5
BATCH = 1_000


def page_entry(size: int = 450 * 1024) -> dict[str, Any]:
    """The shape Talk Python's rendered_cache stores for show pages and RSS feeds."""
    body = random.Random(42).randbytes(size)  # Incompressible, so the stored size is ~size.
    return {'content_gz': body, 'created': '2026-10-27T12:00:00+00:00', 'etag': 'abc123', 'version': 2}


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
    """Median-of-TRIALS p50 (batched, harness overhead subtracted) and p99 (per-call, timer overhead included), in microseconds."""
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

    return {'p50_us': statistics.median(p50s) / 1_000, 'p99_us': statistics.median(p99s) / 1_000}


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


LOOP_OVERHEAD_NS = 0.0


def run(backend: Backend, quick: bool) -> dict[str, Any]:
    global LOOP_OVERHEAD_NS
    LOOP_OVERHEAD_NS = loop_overhead_ns()
    results: dict[str, Any] = {'harness_loop_overhead_ns': LOOP_OVERHEAD_NS}
    n_batches = 20 if quick else 100
    n_samples = 2_000 if quick else 10_000
    scan_sizes = [100_000] if quick else [100_000, 400_000]

    with tempfile.TemporaryDirectory(prefix='dco-bench-') as tmp:
        cache = backend.make_cache(Path(tmp) / 'latency', timeout=1.0, size_limit=2**30)

        small = 'x' * 100
        cache.set('small', small)
        results['get_hit_small'] = measure(lambda i: cache.get('small'), n_batches=n_batches, n_samples=n_samples, warmup=5_000)
        results['get_miss'] = measure(lambda i: cache.get('missing'), n_batches=n_batches, n_samples=n_samples, warmup=5_000)

        entry = page_entry()
        cache.set('page', entry, expire=3600)
        results['get_page_entry_450k'] = measure(lambda i: cache.get('page'), n_batches=0, n_samples=200 if quick else 1_000, warmup=50)
        results['set_page_entry_450k'] = measure(
            lambda i: cache.set(f'page:{i % 50}', entry, expire=3600), n_batches=0, n_samples=200 if quick else 1_000, warmup=50
        )

        def rate_limit_bump(i: int) -> None:
            key = f'h:{i % 1_000}:10.0.0.{i % 256}'
            cache.add(key, 0, expire=3600)
            cache.incr(key)

        results['add_incr_counter'] = measure(rate_limit_bump, n_batches=n_batches // 4 or 1, n_samples=n_samples // 4, warmup=1_000)
        results['threads_8_mixed_ops_per_s'] = threaded_throughput(cache, seconds=1.0 if quick else 3.0)
        cache.close()

        for size in scan_sizes:
            scan_cache = backend.make_cache(Path(tmp) / f'scan-{size}', timeout=10.0, size_limit=2**32)
            populate(scan_cache, size)
            label = f'{size // 1_000}k'
            results[f'iterkeys_prefix_scan_{label}'] = measure_once(lambda: [k for k in scan_cache.iterkeys() if k.startswith('d:')])
            results[f'len_{label}'] = measure_once(lambda: len(scan_cache))
            results[f'volume_{label}'] = measure_once(lambda: scan_cache.volume())
            scan_cache.close()

    return results


def threaded_throughput(cache: Any, *, seconds: float, threads: int = 8) -> dict[str, float]:
    """8 threads sharing one cache, 90% get / 10% set of ~1 KB values, for `seconds`."""
    value = 'v' * 1_024
    for i in range(1_000):
        cache.set(f't:{i}', value)

    counts = [0] * threads
    stop = threading.Event()
    barrier = threading.Barrier(threads + 1)

    def worker(n: int) -> None:
        rng = random.Random(n)
        barrier.wait()
        ops = 0
        while not stop.is_set():
            key = f't:{rng.randrange(1_000)}'
            if rng.random() < 0.1:
                cache.set(key, value)
            else:
                cache.get(key)
            ops += 1
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
    elapsed = time.perf_counter() - start
    return {'ops_per_s': sum(counts) / elapsed, 'threads': threads}


def power_source() -> str:
    if sys.platform != 'darwin':
        return 'unknown'
    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return 'unknown'
    return 'ac' if 'AC Power' in out else 'battery' if 'Battery Power' in out else 'unknown'


def metadata(backend: Backend, machine: str, quick: bool) -> dict[str, Any]:
    gil_disabled = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
    return {
        'backend': backend.name,
        'backend_version': getattr(backend, '_module').__version__ if hasattr(getattr(backend, '_module'), '__version__') else None,
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
    parser.add_argument('--machine', default=platform.node().split('.')[0] or 'unknown', help='Label for the results file.')
    parser.add_argument('--quick', action='store_true', help='Smaller run for smoke-testing the script.')
    parser.add_argument('--out', type=Path, help='Override the output path.')
    args = parser.parse_args()

    backend = BACKENDS[args.backend]()
    meta = metadata(backend, args.machine, args.quick)
    results = run(backend, args.quick)

    out = args.out or RESULTS_DIR / f'{meta["backend"]}-{meta["python"]}-{meta["machine"]}{"-quick" if args.quick else ""}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'meta': meta, 'results': results}, indent=2) + '\n')
    print(json.dumps(results, indent=2))
    print(f'Wrote {out}')


if __name__ == '__main__':
    main()
