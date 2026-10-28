"""Experiment 5 (diskcache side): capture the SQL diskcache 5.6.3 runs for iterkeys, EXPLAIN it, and time
iterkeys + startswith filter at several sizes.

Clean room: the SQL is observed with a sqlite3 trace callback (we wrap sqlite3.connect before diskcache
is imported); diskcache's source is never read.

Run: spikes/storage/.venv/bin/python -I spikes/storage/scripts/diskcache_prefix.py <work dir> [sizes...]
"""

import random
import sqlite3
import sys
import time
from pathlib import Path

TRACE: list[str] = []
_connect = sqlite3.connect


def traced_connect(*args, **kwargs):
    con = _connect(*args, **kwargs)
    con.set_trace_callback(TRACE.append)
    return con


sqlite3.connect = traced_connect
import diskcache  # noqa: E402  (must come after the wrap)


def populate(cache, count: int) -> None:
    """Same key shape as bench/baseline.py: '{h|d}:{i % 24}:{ip}:{i}', 25% under 'd:'."""
    rng = random.Random(7)
    for start in range(0, count, 10_000):
        with cache.transact():
            for i in range(start, min(start + 10_000, count)):
                prefix = 'h' if i % 4 else 'd'
                ip = f'{rng.randrange(256)}.{rng.randrange(256)}.{rng.randrange(256)}.{i % 256}'
                cache.set(f'{prefix}:{i % 24}:{ip}:{i}', i, expire=3600)


def main() -> None:
    work = Path(sys.argv[1])
    sizes = [int(s) for s in sys.argv[2:]] or [100_000, 400_000]
    for n in sizes:
        d = work / f'dc-{n}'
        fresh = not (d / 'cache.db').exists()
        cache = diskcache.Cache(str(d), size_limit=2**32)
        if fresh:
            t = time.perf_counter()
            populate(cache, n)
            print(f'populated {n} in {time.perf_counter() - t:.1f}s')
        TRACE.clear()
        it = cache.iterkeys()
        first = [next(it) for _ in range(250)]  # enough for a few pages
        del it, first
        queries = [q for q in TRACE if q.lstrip().upper().startswith('SELECT')]
        if n == sizes[0]:
            print('schema:')
            for (sql,) in _connect(d / 'cache.db').execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"):
                print('   ', ' '.join(sql.split()))
            print('iterkeys SQL observed (first 4 SELECTs):')
            for q in queries[:4]:
                print('   ', q)
            con = _connect(d / 'cache.db')
            for q in dict.fromkeys(queries[:4]):
                plan = [row[3] for row in con.execute(f'EXPLAIN QUERY PLAN {q}')]
                print(f'    EXPLAIN QUERY PLAN -> {" | ".join(plan)}')
            # The same query shape with the OR removed, for comparison.
            page_q = queries[1] if len(queries) > 1 else queries[0]
            print('    (for comparison, a plain range page:)',
                  [row[3] for row in con.execute("EXPLAIN QUERY PLAN SELECT key, raw FROM Cache WHERE key > 'x' ORDER BY key LIMIT 100")])
            con.close()
        best = []
        for _ in range(3 if n <= 100_000 else 1):
            t = time.perf_counter()
            hits = [k for k in cache.iterkeys() if k.startswith('d:')]
            best.append(time.perf_counter() - t)
        TRACE.clear()
        t = time.perf_counter()
        total = sum(1 for _ in cache.iterkeys())
        full = time.perf_counter() - t
        print(f'n={n}: iterkeys + startswith("d:") -> {len(hits)} keys, best {min(best):.2f}s '
              f'(runs: {", ".join(f"{b:.2f}" for b in best)}); plain full iterkeys {total} keys {full:.2f}s; '
              f'SELECTs per full iterkeys: {sum(1 for q in TRACE if q.lstrip().upper().startswith("SELECT"))}')
        cache.close()


if __name__ == '__main__':
    main()
