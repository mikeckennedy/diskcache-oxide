"""Hot-loop reads from threads, on one shared handle or one handle per thread.

Usage (from the repo root):
    venv/bin/python bench/scaling/hot_reads.py THREADS SECONDS shared|own TMPDIR

Scratch directories are created under TMPDIR.
"""

import os
import random
import sys
import tempfile
import threading
import time

import diskcache_oxide as d

threads = int(sys.argv[1])
seconds = float(sys.argv[2])
mode = sys.argv[3]  # shared | own
tmp = sys.argv[4]


def make():
    c = d.Cache(tempfile.mkdtemp(dir=tmp), timeout=1.0, durable=True)
    for i in range(700):
        c.set(f'https://talkpython.fm/static/img/{i:05d}/cover.webp?{i}', f'{i:06x}')
    return c


caches = [make()] if mode == 'shared' else [make() for _ in range(threads)]
counts = [0] * threads
stop = time.monotonic() + seconds


def w(i):
    get = caches[i % len(caches)].get
    r = random.Random(i)
    keys = [f'https://talkpython.fm/static/img/{j:05d}/cover.webp?{j}' for j in r.choices(range(700), k=50_000)]
    n = 0
    while time.monotonic() < stop:
        for k in keys[:5000]:
            get(k)
        n += 5000
    counts[i] = n


print('pid', os.getpid(), flush=True)
ts = [threading.Thread(target=w, args=(i,)) for i in range(threads)]
s = time.monotonic()
[t.start() for t in ts]
[t.join() for t in ts]
print(f'{mode} x{threads}: {sum(counts) / (time.monotonic() - s) / 1000:.0f}k gets/s')
