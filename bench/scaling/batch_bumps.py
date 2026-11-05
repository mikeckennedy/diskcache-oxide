"""Time batched counter bumps in one transaction, stock diskcache versus diskcache-oxide.

Usage (from the repo root):
    venv/bin/python bench/scaling/batch_bumps.py stock|oxide TMPDIR

Scratch directories are created under TMPDIR.
"""

import random
import statistics
import sys
import tempfile
import time

backend, tmp = sys.argv[1], sys.argv[2]
if backend == 'stock':
    import diskcache

    c = diskcache.Cache(tempfile.mkdtemp(dir=tmp), timeout=0.25)
    group = c.transact
else:
    import diskcache_oxide

    c = diskcache_oxide.Cache(tempfile.mkdtemp(dir=tmp), timeout=0.25, durable=True)
    group = c._hold_write_transaction


def bump(key, window):
    seeded = c.add(key, 0, expire=window)
    try:
        return c.incr(key, default=None)
    except KeyError:
        return None if not seeded else c.incr(key, default=0)


ips = [f'10.{i // 65536}.{(i // 256) % 256}.{i % 256}' for i in range(3000)]
picks = random.Random(1).choices(ips, k=6000)


def three(ip):
    bump(f'rl:h:{ip}:2026110512', 3600)
    bump(f'rl:d:{ip}:20261105', 86400)
    bump('rl:stats:modern_chrome:2026110512', 3600)


def grouped(ip):
    with group():
        three(ip)


for warm in picks[:500]:
    three(warm)
res = {}
for name, fn in (('separate', three), ('one transaction', grouped)) * 3:
    s = time.perf_counter()
    for ip in picks:
        fn(ip)
    res.setdefault(name, []).append((time.perf_counter() - s) / len(picks) * 1e6)
print(f'{backend:6}', '  '.join(f'{k}: {statistics.median(v):5.1f} µs per request' for k, v in res.items()))
