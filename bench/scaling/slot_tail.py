"""Tail check for the slot wait (plan 12 step 5, 5b's fairness check).

16 threads reading one shared `FanoutCache(8)` at its default budget (8 connections, one per shard)
for 60 s, `durable=False`, so a slot wait that starves past `--timeout` (default 1 s) raises
`Timeout`. Each thread times every `get` and buckets it (power-of-two buckets split 8 ways, so a
quantile is within 12.5%). The `get` time includes the slot wait, so its p99.9 bounds the slot
wait's.

Usage (from the repo root):
    PY bench/scaling/slot_tail.py TMPDIR [--threads 16] [--seconds 60] [--timeout 1.0]
"""

from __future__ import annotations

import argparse
import json
import random
import tempfile
import threading
import time

KEYS = [f'https://talkpython.fm/static/img/{i:05d}/cover.webp?{i}' for i in range(700)]


def bucket(ns: int) -> int:
    b = ns.bit_length()
    if b <= 4:
        return ns
    return (b << 3) | ((ns >> (b - 4)) & 7)


def lower(key: int) -> int:
    if key < 16:
        return key
    b, sub = key >> 3, key & 7
    return (8 | sub) << (b - 4)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('tmp')
    ap.add_argument('--threads', type=int, default=16)
    ap.add_argument('--seconds', type=float, default=60.0)
    ap.add_argument('--timeout', type=float, default=1.0)
    a = ap.parse_args()

    import diskcache_oxide as d
    from diskcache_oxide import _core

    assert not _core._DEBUG_BUILD, 'measure a release build'
    fan = d.FanoutCache(tempfile.mkdtemp(dir=a.tmp), shards=8, timeout=a.timeout, durable=False)
    for i, k in enumerate(KEYS):
        fan.set(k, f'{i:06x}')

    hists: list[dict[int, int]] = [{} for _ in range(a.threads)]
    maxes = [0] * a.threads
    timeouts = [0] * a.threads
    stop = threading.Event()
    bar = threading.Barrier(a.threads + 1)

    def w(i: int) -> None:
        ks = random.Random(i).choices(KEYS, k=100_000)
        get, clock, hist = fan.get, time.perf_counter_ns, hists[i]
        worst = 0
        bar.wait()
        n = 0
        while not stop.is_set():
            for k in ks[n : n + 1000]:
                s = clock()
                try:
                    get(k)
                except d.Timeout:
                    timeouts[i] += 1
                ns = clock() - s
                worst = max(worst, ns)
                b = bucket(ns)
                hist[b] = hist.get(b, 0) + 1
            n = (n + 1000) % len(ks)
        maxes[i] = worst

    ts = [threading.Thread(target=w, args=(i,)) for i in range(a.threads)]
    for t in ts:
        t.start()
    bar.wait()
    start = time.perf_counter()
    time.sleep(a.seconds)
    stop.set()
    for t in ts:
        t.join()
    elapsed = time.perf_counter() - start

    total: dict[int, int] = {}
    for h in hists:
        for k, v in h.items():
            total[k] = total.get(k, 0) + v
    count = sum(total.values())

    def quantile(q: float) -> float:
        need, seen = q * count, 0
        for k in sorted(total):
            seen += total[k]
            if seen >= need:
                return lower(k) / 1000
        return max(maxes) / 1000

    print(
        json.dumps(
            {
                'threads': a.threads,
                'timeout_s': a.timeout,
                'seconds': round(elapsed, 1),
                'gets': count,
                'k_gets_per_s': round(count / elapsed / 1000),
                'timeouts': sum(timeouts),
                'p50_us': quantile(0.5),
                'p99_us': quantile(0.99),
                'p999_us': quantile(0.999),
                'p9999_us': quantile(0.9999),
                'max_us': round(max(maxes) / 1000, 1),
            }
        )
    )


if __name__ == '__main__':
    main()
