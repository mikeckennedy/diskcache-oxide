"""Hammer incr on a small set of hot keys in diskcache-oxide.

Usage (from the repo root):
    venv/bin/python bench/scaling/hot_incr.py TMPDIR SECONDS

Scratch directories are created under TMPDIR.
"""

import os
import random
import sys
import tempfile
import time

import diskcache_oxide as d

c = d.Cache(tempfile.mkdtemp(dir=sys.argv[1]), timeout=0.25, durable=True)
keys = [f'rl:h:10.0.{i // 256}.{i % 256}:2026110512' for i in range(3000)]
for k in keys:
    c.add(k, 0, expire=3600)
picks = random.Random(1).choices(keys, k=20000)
print('pid', os.getpid(), flush=True)
stop = time.monotonic() + float(sys.argv[2])
n = 0
s = time.monotonic()
while time.monotonic() < stop:
    for k in picks:
        c.incr(k, default=None)
    n += len(picks)
print(f'{n / (time.monotonic() - s) / 1000:.1f}k incr/s, {(time.monotonic() - s) / n * 1e6:.1f} us each')
