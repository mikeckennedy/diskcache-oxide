"""iterkeys() takes quadratic time: 4x the keys costs about 16x the time.

Each page of iterkeys() is fetched with a query whose OR condition stops SQLite from using the
key index when the values are bound as parameters, so every page scans the whole table.
"""

import tempfile
import time

import diskcache


def scan_seconds(count: int) -> float:
    with tempfile.TemporaryDirectory() as tmp, diskcache.Cache(tmp) as cache:
        with cache.transact():
            for i in range(count):
                cache.set(f'key:{i:08d}', i)
        start = time.perf_counter()
        n = sum(1 for _ in cache.iterkeys())
        elapsed = time.perf_counter() - start
        assert n == count
        return elapsed


small, large = scan_seconds(20_000), scan_seconds(80_000)
ratio = large / small
print(
    f'iterkeys over 20k keys: {small:.3f} s | over 80k keys: {large:.3f} s | ratio {ratio:.1f}x (linear would be ~4x)'
)
bug = ratio > 8
print('BUG REPRODUCED (super-linear)' if bug else 'not reproduced')
raise SystemExit(1 if bug else 0)
