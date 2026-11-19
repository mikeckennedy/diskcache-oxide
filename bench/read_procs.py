"""Reads only, across worker processes: does a plain Cache's multi-process fall come from writes?

Usage (from the repo root, on AC power, nothing else heavy running, not under nice):
    venv/bin/python -m bench.read_procs
    venv/bin/python -m bench.read_procs --quick

Each spawned process opens the cache and calls `get` in a share-nothing loop over 1,000 keys of a
1 KB str, with no writer anywhere, at 1, 2, 4 and 8 processes. Variants: oxide Cache, oxide
FanoutCache(8), diskcache 5.6.3's FanoutCache(8). Rounds alternate the variants; the median round is
reported. Writes bench/results/read-procs-<python>-<machine>.{json,md}. Plan 31's step 6 review
found that a plain Cache falls past two processes even here, so the fall is on the read side.
"""

from __future__ import annotations

import argparse
import json
import multiprocessing
import os
import platform
import queue
import random
import shutil
import statistics
import sys
import sysconfig
import tempfile
import time
from pathlib import Path
from typing import Any

RESULTS_DIR = Path(__file__).parent / 'results'
COUNTS = (1, 2, 4, 8)
KEYS = 1_000
VARIANTS = ('oxide Cache', 'oxide FanoutCache(8)', 'diskcache FanoutCache(8)')


def open_cache(variant: str, directory: str) -> Any:
    if variant == 'oxide Cache':
        import diskcache_oxide

        return diskcache_oxide.Cache(directory)
    if variant == 'oxide FanoutCache(8)':
        import diskcache_oxide

        return diskcache_oxide.FanoutCache(directory, shards=8)
    import diskcache

    return diskcache.FanoutCache(directory, shards=8)


def worker(variant: str, directory: str, seed: int, ready: Any, go: Any, stop: Any, results: Any) -> None:
    cache = open_cache(variant, directory)
    keys = [''.join(('t:', str(i))) for i in range(KEYS)]
    rng = random.Random(seed)
    plan = [keys[rng.randrange(KEYS)] for _ in range(1_000)]
    get = cache.get
    get(keys[0])  # open before the clock starts
    ready.release()
    go.wait()
    ops = 0
    while not stop.is_set():
        for key in plan:
            get(key)
        ops += len(plan)
    results.put(ops)
    cache.close()


def measure(variant: str, directory: str, count: int, seconds: float) -> float:
    context = multiprocessing.get_context('spawn')
    ready, go, stop, results = context.Semaphore(0), context.Event(), context.Event(), context.Queue()
    pool = [
        context.Process(target=worker, args=(variant, directory, n, ready, go, stop, results)) for n in range(count)
    ]
    for process in pool:
        process.start()
    for _ in pool:
        if not ready.acquire(timeout=120):
            stop.set()
            go.set()
            raise RuntimeError(f'{variant}: a worker never got ready')
    go.set()
    start = time.perf_counter()
    time.sleep(seconds)
    stop.set()
    try:
        total = sum(results.get(timeout=seconds + 120) for _ in pool)
    except queue.Empty:
        raise RuntimeError(f'{variant}: a worker never reported')
    elapsed = time.perf_counter() - start
    for process in pool:
        process.join()
    return total / elapsed


def power_source() -> str:
    if sys.platform != 'darwin':
        return 'unknown'
    import subprocess

    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True, text=True, timeout=5).stdout
    except OSError, subprocess.SubprocessError:
        return 'unknown'
    return 'ac' if 'AC Power' in out else 'battery' if 'Battery Power' in out else 'unknown'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--machine', default=platform.node().split('.')[0] or 'unknown')
    parser.add_argument('--quick', action='store_true', help='Smoke-test the script; the numbers mean nothing.')
    parser.add_argument('--rounds', type=int, help='Rounds per variant (default 3, quick 1).')
    parser.add_argument('--seconds', type=float, help='Seconds per point (default 1.5, quick 0.2).')
    args = parser.parse_args()
    rounds = args.rounds or (1 if args.quick else 3)
    seconds = args.seconds or (0.2 if args.quick else 1.5)
    gil_disabled = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
    meta = {
        'python': platform.python_version() + ('t' if gil_disabled else ''),
        'machine': args.machine,
        'platform': platform.platform(),
        'power': power_source(),
        'nice': os.getpriority(os.PRIO_PROCESS, 0),
        'quick': args.quick,
        'rounds': rounds,
        'seconds_per_point': seconds,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
    }
    if not args.quick and (meta['power'] == 'battery' or meta['nice'] > 0):
        raise SystemExit(f'refusing a full run on {meta["power"]} power at nice {meta["nice"]}; use --quick')

    root = Path(tempfile.mkdtemp(prefix='dco-read-procs-'))
    samples: dict[str, dict[str, list[float]]] = {v: {str(c): [] for c in COUNTS} for v in VARIANTS}
    try:
        directories = {}
        for n, variant in enumerate(VARIANTS):
            directories[variant] = str(root / str(n))
            cache = open_cache(variant, directories[variant])
            for i in range(KEYS):
                cache.set(f't:{i}', 'v' * 1_024)
            cache.close()
        for r in range(rounds):
            for variant in VARIANTS:
                for count in COUNTS:
                    samples[variant][str(count)].append(measure(variant, directories[variant], count, seconds))
                print(f'round {r + 1}/{rounds}: {variant} done', file=sys.stderr)
    finally:
        shutil.rmtree(root, ignore_errors=True)

    results = {v: {c: statistics.median(s) for c, s in by_count.items()} for v, by_count in samples.items()}
    lines = [
        f'Reads only, {KEYS:,} keys of a 1 KB str, one thread per process, median of {rounds} rounds. '
        f'Python {meta["python"]}, {meta["machine"]}, {meta["power"]} power.\n',
        '| Processes | ' + ' | '.join(VARIANTS) + ' |',
        '|---' * (len(VARIANTS) + 1) + '|',
    ]
    for count in COUNTS:
        lines.append(f'| {count} | ' + ' | '.join(f'{results[v][str(count)] / 1_000:,.0f}k' for v in VARIANTS) + ' |')
    text = '\n'.join(lines) + '\n'
    out = RESULTS_DIR / f'read-procs-{meta["python"]}-{meta["machine"]}{"-quick" if args.quick else ""}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'meta': meta, 'samples': samples, 'results': results}, indent=2) + '\n')
    out.with_suffix('.md').write_text(text)
    print(text)
    print(f'Wrote {out}')


if __name__ == '__main__':
    main()
