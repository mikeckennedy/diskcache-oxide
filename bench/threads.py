"""Threads in one process: how oxide scales on free-threaded and GIL builds (plan 34, step 1).

Usage (from the repo root, on AC power, nothing else heavy running, not under nice):
    venv/bin/python -m bench.threads --arm 3.14=venv/bin/python --arm 3.14t=.venv-ft/bin/python
    venv/bin/python -m bench.threads --arm 3.14t=.venv-ft/bin/python --quick
    venv/bin/python -m bench.threads --arm ... --counts 1,2,4,8 --rounds 5 --only 'oxide Cache, shared'

Each arm is an interpreter with diskcache-oxide (and diskcache 5.6.3) installed. Every variant runs
in a fresh process of each arm, and the rounds interleave arms and variants, so a slow minute on the
machine lands on all of them. In each process the variant runs every mix at every thread count, in
the share-nothing loop of `bench.baseline` (each thread builds its own keys and plan and binds its
methods once; the stop flag is read between rounds of 1,000 calls).

Variants:
- oxide `Cache` and `FanoutCache(8)`, one handle shared by every thread, and a handle per thread on
  the same directory;
- the control: a `Cache` per thread on its own directory, where nothing is shared. Plan 34's
  targets are a share of this row at the same thread count;
- a getter on a shared `Cache` (`cache.timeout`): a call into `_core` that touches no store, for
  what the Python boundary costs on its own;
- a Python dict lookup: the harness's own ceiling;
- diskcache 5.6.3's `Cache` and `FanoutCache(8)`, one shared handle.

Mixes: reads only, 1,000 reads : 1 write (the Talk Python profile) and 99 : 1 (the headline), on
1,000 keys of a 1 KB str. The getter and dict rows run reads only. The median round is reported.
Writes bench/results/threads-<machine>.{json,md}.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import random
import resource
import shutil
import statistics
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

RESULTS_DIR = Path(__file__).parent / 'results'
KEYS = 1_000
VALUE = 'v' * 1_024
MIXES = {'reads': 0, '1000:1': 1_000, '99:1': 100}  # one write in every N calls; 0 is none
CEILING = 'oxide Cache, directory per thread'
VARIANTS = (
    'oxide Cache, shared',
    'oxide Cache, handle per thread',
    'oxide FanoutCache(8), shared',
    'oxide FanoutCache(8), handle per thread',
    CEILING,
    'oxide getter, shared Cache',
    'dict lookup',
    'diskcache Cache, shared',
    'diskcache FanoutCache(8), shared',
)
# The share of the process's CPU time spent in the kernel at each point, by (threads, every).
KERNEL_SHARE: dict[tuple[int, int], float] = {}
READS_ONLY = {'oxide getter, shared Cache', 'dict lookup'}


def open_cache(variant: str, directory: str) -> Any:
    if variant.startswith('diskcache'):
        import diskcache

        return diskcache.FanoutCache(directory, shards=8) if 'Fanout' in variant else diskcache.Cache(directory)
    import diskcache_oxide

    return diskcache_oxide.FanoutCache(directory, shards=8) if 'Fanout' in variant else diskcache_oxide.Cache(directory)


def throughput(
    make_ops: Callable[[int], tuple[Callable[[str], Any], Callable[[str, str], Any]]],
    threads: int,
    every: int,
    seconds: float,
) -> float:
    """Calls a second over `threads` threads; `make_ops(n)` gives thread n its (get, set)."""
    counts = [0] * threads
    stop = threading.Event()
    barrier = threading.Barrier(threads + 1)

    def worker(n: int) -> None:
        get, put = make_ops(n)
        keys = [''.join(('t:', str(i))) for i in range(KEYS)]
        rng = random.Random(n)
        size = every * max(1, 1_000 // every) if every else 1_000
        plan = [(keys[rng.randrange(KEYS)], bool(every) and i % every == 0) for i in range(size)]
        value = ''.join(('v',) * len(VALUE))
        barrier.wait()
        ops = 0
        while not stop.is_set():
            for key, write in plan:
                if write:
                    put(key, value)
                else:
                    get(key)
            ops += len(plan)
        counts[n] = ops

    pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
    for thread in pool:
        thread.start()
    barrier.wait()
    start = time.perf_counter()
    before = resource.getrusage(resource.RUSAGE_SELF)
    time.sleep(seconds)
    stop.set()
    for thread in pool:
        thread.join()
    after = resource.getrusage(resource.RUSAGE_SELF)
    elapsed = time.perf_counter() - start
    user, system = after.ru_utime - before.ru_utime, after.ru_stime - before.ru_stime
    KERNEL_SHARE[(threads, every)] = system / (user + system) if user + system else 0.0
    return sum(counts) / elapsed


def run_variant(
    variant: str, counts: list[int], seconds: float, only_mixes: Optional[list[str]] = None
) -> dict[str, dict[str, float]]:
    """One variant at every mix and thread count, in this process."""
    root = Path(tempfile.mkdtemp(prefix='dco-threads-'))
    caches: list[Any] = []
    try:
        if variant == 'dict lookup':
            table = {f't:{i}': VALUE for i in range(KEYS)}

            def make_ops(n: int) -> tuple[Callable[[str], Any], Callable[[str, str], Any]]:
                own = dict(table)
                return own.get, own.__setitem__
        else:
            per_dir = variant == CEILING
            directories = [str(root / str(n)) for n in range(max(counts) if per_dir else 1)]
            for directory in directories:
                cache = open_cache(variant, directory)
                for i in range(KEYS):
                    cache.set(f't:{i}', VALUE)
                caches.append(cache)
            if variant.endswith('handle per thread'):
                caches += [open_cache(variant, directories[0]) for _ in range(max(counts) - 1)]
            if variant == 'oxide getter, shared Cache':
                shared = caches[0]

                def make_ops(n: int) -> tuple[Callable[[str], Any], Callable[[str, str], Any]]:
                    def get(key: str) -> Any:
                        return shared.timeout

                    return get, shared.set
            else:
                one = not (per_dir or variant.endswith('handle per thread'))

                def make_ops(n: int) -> tuple[Callable[[str], Any], Callable[[str, str], Any]]:
                    cache = caches[0] if one else caches[n]
                    return cache.get, cache.set

        mixes = {'reads': 0} if variant in READS_ONLY else MIXES
        mixes = {m: e for m, e in mixes.items() if not only_mixes or m in only_mixes}
        rates = {mix: {str(c): throughput(make_ops, c, every, seconds) for c in counts} for mix, every in mixes.items()}
        kernel = {f'{mix} kernel': {str(c): KERNEL_SHARE[(c, every)] for c in counts} for mix, every in mixes.items()}
        return rates | kernel
    finally:
        for cache in caches:
            cache.close()
        shutil.rmtree(root, ignore_errors=True)


def describe() -> dict[str, Any]:
    import diskcache_oxide

    gil_disabled = bool(sysconfig.get_config_var('Py_GIL_DISABLED'))
    return {
        'python': platform.python_version() + ('t' if gil_disabled else ''),
        'oxide': diskcache_oxide.__version__,
        'oxide_path': diskcache_oxide.__file__,
    }


def power_source() -> str:
    if sys.platform != 'darwin':
        return 'unknown'
    try:
        out = subprocess.run(['pmset', '-g', 'batt'], capture_output=True, text=True, timeout=5).stdout
    except OSError, subprocess.SubprocessError:
        return 'unknown'
    return 'ac' if 'AC Power' in out else 'battery' if 'Battery Power' in out else 'unknown'


def table(title: str, rows: list[tuple[str, dict[str, float]]], counts: list[int], cell: Callable[[float], str]) -> str:
    lines = [
        f'{title}\n',
        '| Arm, variant | ' + ' | '.join(str(c) for c in counts) + ' |',
        '|---' * (len(counts) + 1) + '|',
    ]
    lines += [f'| {name} | ' + ' | '.join(cell(row[str(c)]) for c in counts) + ' |' for name, row in rows]
    return '\n'.join(lines) + '\n'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        '--arm', action='append', default=[], help='NAME=PYTHON (repeatable); default: this interpreter.'
    )
    parser.add_argument('--counts', default='1,2,4,8', help='Thread counts (default 1,2,4,8).')
    parser.add_argument('--rounds', type=int, help='Interleaved rounds (default 5, quick 1).')
    parser.add_argument('--seconds', type=float, help='Seconds per point (default 1.0, quick 0.2).')
    parser.add_argument('--only', action='append', help='Only variants starting with this (repeatable).')
    parser.add_argument('--quick', action='store_true', help='Smoke-test the script; the numbers mean nothing.')
    parser.add_argument('--machine', default=platform.node().split('.')[0] or 'unknown')
    parser.add_argument('--out', type=Path, default=RESULTS_DIR)
    parser.add_argument('--worker', help=argparse.SUPPRESS)
    parser.add_argument('--mixes', help=argparse.SUPPRESS)  # the worker's mixes, comma separated (bench.gate)
    args = parser.parse_args()
    counts = [int(c) for c in args.counts.split(',')]
    seconds = args.seconds or (0.2 if args.quick else 1.0)
    if args.worker:
        print(
            json.dumps(
                {
                    'meta': describe(),
                    'results': run_variant(args.worker, counts, seconds, args.mixes.split(',') if args.mixes else None),
                }
            )
        )
        return

    rounds = args.rounds or (1 if args.quick else 5)
    arms = dict(a.split('=', 1) for a in args.arm) or {'this': sys.executable}
    variants = [v for v in VARIANTS if not args.only or any(v.startswith(o) for o in args.only)]
    meta: dict[str, Any] = {
        'machine': args.machine,
        'platform': platform.platform(),
        'cpus': os.cpu_count(),
        'power': power_source(),
        'nice': os.getpriority(os.PRIO_PROCESS, 0),
        'rounds': rounds,
        'seconds_per_point': seconds,
        'quick': args.quick,
        'timestamp': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        'arms': {},
    }
    if not args.quick and (meta['power'] == 'battery' or meta['nice'] > 0):
        raise SystemExit(f'refusing a full run on {meta["power"]} power at nice {meta["nice"]}; use --quick')
    samples: dict[str, dict[str, dict[str, dict[str, list[float]]]]] = {a: {} for a in arms}
    loads = []
    for r in range(rounds):
        for variant in variants:
            for arm, python in arms.items():
                loads.append(os.getloadavg()[0])
                command = [
                    python,
                    '-m',
                    'bench.threads',
                    '--worker',
                    variant,
                    '--counts',
                    args.counts,
                    '--seconds',
                    str(seconds),
                ]
                out = subprocess.run(
                    command, capture_output=True, text=True, check=True, cwd=Path(__file__).parent.parent
                )
                got = json.loads(out.stdout.strip().splitlines()[-1])
                meta['arms'][arm] = got['meta']
                for mix, by_count in got['results'].items():
                    slot = samples[arm].setdefault(variant, {}).setdefault(mix, {})
                    for c, rate in by_count.items():
                        slot.setdefault(c, []).append(rate)
            print(f'round {r + 1}/{rounds}: {variant} done', file=sys.stderr)
    meta['load_average'] = {'min': min(loads), 'max': max(loads)}

    medians = {
        a: {v: {m: {c: statistics.median(s) for c, s in bc.items()} for m, bc in bm.items()} for v, bm in bv.items()}
        for a, bv in samples.items()
    }
    parts = [
        f'Threads in one process, 1,000 keys of a 1 KB str, median of {rounds} rounds of {seconds} s, thousands of '
        f'calls a second. {meta["machine"]} ({meta["cpus"]} CPUs), {meta["power"]} power. Arms: '
        + '; '.join(f'{a}: Python {m["python"]}, oxide {m["oxide"]}' for a, m in meta['arms'].items())
        + '.\n'
    ]
    for mix in MIXES:
        rows = [(f'{a}, {v}', medians[a][v][mix]) for v in variants for a in arms if mix in medians[a].get(v, {})]
        parts.append(table(f'**{mix}**, calls a second:', rows, counts, lambda x: f'{x / 1_000:,.0f}k'))
        share = [
            (f'{a}, {v}', {c: row[c] / medians[a][CEILING][mix][c] for c in row})
            for a in arms
            for v in variants
            if v != CEILING and CEILING in medians[a] and mix in medians[a].get(v, {})
            for row in [medians[a][v][mix]]
        ]
        if share:
            parts.append(table(f'**{mix}**, as a share of "{CEILING}":', share, counts, lambda x: f'{100 * x:.0f}%'))
        kernel = [
            (f'{a}, {v}', medians[a][v][f'{mix} kernel'])
            for v in variants
            for a in arms
            if f'{mix} kernel' in medians[a].get(v, {})
        ]
        if kernel:
            parts.append(
                table(f'**{mix}**, share of CPU time in the kernel:', kernel, counts, lambda x: f'{100 * x:.0f}%')
            )
    text = '\n'.join(parts)
    out = args.out / f'threads-{meta["machine"]}{"-quick" if args.quick else ""}.json'
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({'meta': meta, 'samples': samples, 'medians': medians}, indent=2) + '\n')
    out.with_suffix('.md').write_text(text)
    print(text)
    print(f'Wrote {out}')


if __name__ == '__main__':
    main()
