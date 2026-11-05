"""Sweep max_connections on diskcache-oxide with 4 threads reading.

Usage (from the repo root):
    venv/bin/python bench/scaling/conn_sweep.py TMPDIR

Scratch directories are created under TMPDIR.
"""

import random
import sys
import tempfile
import threading
import time

import diskcache_oxide as d

tmp = sys.argv[1]
N = 60_000
KEYS = [f'https://talkpython.fm/static/img/{i:05d}/cover.webp?{i}' for i in range(700)]


def make(**kw):
    c = d.Cache(tempfile.mkdtemp(dir=tmp), timeout=1.0, durable=True, **kw)
    for i, k in enumerate(KEYS):
        c.set(k, f'{i:06x}')
    return c


def run(c, threads):
    lists = [[KEYS[j] for j in random.Random(t).choices(range(700), k=N)] for t in range(threads)]
    bar = threading.Barrier(threads + 1)

    def w(i):
        get = c.get
        ks = lists[i]
        bar.wait()
        for k in ks:
            get(k)

    ts = [threading.Thread(target=w, args=(i,)) for i in range(threads)]
    [t.start() for t in ts]
    bar.wait()
    s = time.perf_counter()
    [t.join() for t in ts]
    return threads * N / (time.perf_counter() - s) / 1000


configs = [4, 8, 16, 32, 64]
caches = {m: make(max_connections=m) for m in configs}
results = {m: [] for m in configs}
for rnd in range(3):
    for m in configs if rnd % 2 == 0 else configs[::-1]:
        results[m].append(run(caches[m], 4))
for m in configs:
    runs = ', '.join(f'{x:.0f}' for x in results[m])
    print(f'max_connections={m:3d}: 4 threads {sorted(results[m])[1]:7.0f}k gets/s  (runs {runs})')
