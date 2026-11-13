"""Before/after table for plan 12 step 5 (build-order step 4) and the M2 report.

Each arm is an interpreter with a diskcache-oxide release build installed. Each arm runs in a fresh
process, arms are interleaved round by round, and the table reports each case's median and min-max,
thousands of gets/s. 6-byte values, 700 URL-shaped keys, reads only, one shared handle per case
(`c16`/`c32`: FanoutCache(8) at 16 or 32 connections), except `dirs4 4t`, the control: four plain
`Cache`s on four directories, one per thread.

Unlike slot_wait.py this works on builds from before step 5 (no `_sqlite_mutex_status`).

Usage (from the repo root):
    PY bench/scaling/m2_probe.py TMPDIR --arm before=/path/bin/python --arm after=/path/bin/python \
        [--rounds 5] [--json out.json]
    PY bench/scaling/m2_probe.py TMPDIR --hold 'cache 4t' [--seconds 30]   # for sample / perf
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time


def load1() -> float:
    """The one-minute load average; Windows has none."""
    return math.nan if sys.platform == 'win32' else os.getloadavg()[0]


N = 60_000
KEYS = [f'https://talkpython.fm/static/img/{i:05d}/cover.webp?{i}' for i in range(700)]
CASES = (
    'cache 1t',
    'cache 4t',
    'fanout8 1t',
    'fanout8 4t',
    'fanout8 c16 4t',
    'fanout8 c32 1t',
    'fanout8 c32 4t',
    'dirs4 4t',
)


def build(tmp: str):
    import diskcache_oxide as d
    from diskcache_oxide import _core

    assert not _core._DEBUG_BUILD, 'measure a release build'

    def fill(c):
        for i, k in enumerate(KEYS):
            c.set(k, f'{i:06x}')
        return c

    def new(cls, **kw):
        return fill(cls(tempfile.mkdtemp(dir=tmp), timeout=1.0, **kw))

    plain = new(d.Cache)
    return {
        'cache': [plain],
        'fanout8': [new(d.FanoutCache, shards=8)],
        'fanout8 c16': [new(d.FanoutCache, shards=8, max_connections=16)],
        'fanout8 c32': [new(d.FanoutCache, shards=8, max_connections=32)],
        'dirs4': [plain] + [new(d.Cache) for _ in range(3)],
    }


def run(caches: list, threads: int, stop_at: float | None = None) -> float:
    lists = [random.Random(t).choices(KEYS, k=N) for t in range(threads)]
    bar = threading.Barrier(threads + 1)
    done = [0] * threads

    def w(i: int) -> None:
        get, ks = caches[i % len(caches)].get, lists[i]
        bar.wait()
        while True:
            for k in ks:
                get(k)
            done[i] += len(ks)
            if stop_at is None or time.perf_counter() >= stop_at:
                return

    ts = [threading.Thread(target=w, args=(i,)) for i in range(threads)]
    for t in ts:
        t.start()
    bar.wait()
    s = time.perf_counter()
    for t in ts:
        t.join()
    return sum(done) / (time.perf_counter() - s) / 1000


def split(case: str) -> tuple[str, int]:
    name, _, threads = case.rpartition(' ')
    return name, int(threads.rstrip('t'))


def child(tmp: str) -> None:
    from diskcache_oxide import _core

    built = build(tmp)
    run(built['cache'], 4)  # warm-up
    out: dict = {'mutex': getattr(_core, '_sqlite_mutex_status', lambda: 'none')()}
    for case in CASES:
        name, threads = split(case)
        out[case] = run(built[name], threads)
    print(json.dumps(out))


def hold(tmp: str, case: str, seconds: float) -> None:
    built = build(tmp)
    name, threads = split(case)
    print(f'pid {os.getpid()} holding {case!r} for {seconds:.0f} s', flush=True)
    rate = run(built[name], threads, stop_at=time.perf_counter() + seconds)
    print(f'{case}: {rate:,.0f}k gets/s', flush=True)


def parse_arm(spec: str) -> tuple[str, str, dict[str, str]]:
    """`name=python` or `name=python:KEY=V,KEY=V` (environment overrides for a prototype build)."""
    name, rest = spec.split('=', 1)
    python, _, envs = rest.partition(':')
    return name, python, dict(kv.split('=', 1) for kv in envs.split(',') if kv)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('tmp')
    ap.add_argument('--arm', action='append', default=[], metavar='NAME=PYTHON[:KEY=V,...]')
    ap.add_argument('--rounds', type=int, default=5)
    ap.add_argument('--json', help='Also write every sample here.')
    ap.add_argument('--hold', choices=CASES, help='Run one case continuously in this process (for profiling).')
    ap.add_argument('--seconds', type=float, default=30.0)
    ap.add_argument('--child', action='store_true')
    a = ap.parse_args()
    if a.child:
        child(a.tmp)
        return
    if a.hold:
        with tempfile.TemporaryDirectory(dir=a.tmp) as tmp:
            hold(tmp, a.hold, a.seconds)
        return
    arms = [parse_arm(spec) for spec in a.arm]
    results: dict[str, list[dict]] = {name: [] for name, _, _ in arms}
    loads = []
    for r in range(a.rounds):
        loads.append(round(load1(), 2))
        for name, python, overrides in arms:
            env = {k: v for k, v in os.environ.items() if not k.startswith('DCO_')}
            env.update(overrides)
            with tempfile.TemporaryDirectory(dir=a.tmp) as tmp:
                proc = subprocess.run(
                    [python, __file__, tmp, '--child'], env=env, capture_output=True, text=True, timeout=900
                )
            if proc.returncode:
                sys.exit(f'{name}: exit {proc.returncode}\n{proc.stderr}')
            row = json.loads(proc.stdout.strip().splitlines()[-1])
            mutex = row.pop('mutex')
            results[name].append(row)
            line = ', '.join(f'{k} {v:,.0f}' for k, v in row.items())
            print(f'round {r + 1} {name:>8} (mutex {mutex}, load {load1():.1f}): {line}', flush=True)
    names = [name for name, _, _ in arms]
    print(f'\nMedian (min-max) of {a.rounds} rounds, thousands of gets/s; 1-min load before each round: {loads}')
    print('| Case | ' + ' | '.join(names) + ' |')
    print('|---|' + '---|' * len(names))
    for case in CASES:
        cells = []
        for name in names:
            vals = [row[case] for row in results[name]]
            cells.append(f'{statistics.median(vals):,.0f} ({min(vals):,.0f}-{max(vals):,.0f})')
        print(f'| {case} | ' + ' | '.join(cells) + ' |')
    if a.json:
        with open(a.json, 'w') as f:
            json.dump({'loads': loads, 'cpus': os.cpu_count(), 'results': results}, f, indent=1)


if __name__ == '__main__':
    main()
