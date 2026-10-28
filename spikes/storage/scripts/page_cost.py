"""Experiment 5: why is diskcache's iterkeys quadratic when EXPLAIN QUERY PLAN shows an index SEARCH?

Measures, per single page query, the time and the number of SQLite VM instructions (via a progress
handler), at cursors near the start, middle and end of the key order, for the observed diskcache query
shape and for alternatives. Works on a cache.db made by diskcache_prefix.py; only plain sqlite3 is used.

Run: python -I page_cost.py <path to cache.db>
"""

import sqlite3
import sys
import time

con = sqlite3.connect(sys.argv[1])
keys = [k for (k,) in con.execute('SELECT key FROM Cache ORDER BY key')]
n = len(keys)
steps = 0


def tick() -> int:
    global steps
    steps += 1
    return 0


con.set_progress_handler(tick, 100)  # one tick per 100 VM instructions

QUERIES = {
    'diskcache (key = ? AND raw > ? OR key > ?)': ('SELECT key, raw FROM Cache WHERE key = ? AND raw > ? OR key > ? '
                                                    'ORDER BY key ASC, raw ASC LIMIT 100', lambda k: (k, 1, k)),
    'row value ((key, raw) > (?, ?))': ('SELECT key, raw FROM Cache WHERE (key, raw) > (?, ?) ORDER BY key ASC, raw ASC LIMIT 100',
                                         lambda k: (k, 1)),
    'plain (key > ?)': ('SELECT key, raw FROM Cache WHERE key > ? ORDER BY key ASC, raw ASC LIMIT 100', lambda k: (k,)),
}
print(f'{n} keys; sqlite {sqlite3.sqlite_version}')
for label, (sql, params) in QUERIES.items():
    plan = ' | '.join(r[3] for r in con.execute(f'EXPLAIN QUERY PLAN {sql}', params(keys[0])))
    print(f'{label}: plan = {plan}')
    for frac in (0.0, 0.5, 0.99):
        k = keys[int(frac * (n - 1))]
        steps = 0
        t = time.perf_counter()
        for _ in range(5):
            rows = con.execute(sql, params(k)).fetchall()
        dt = (time.perf_counter() - t) / 5
        print(f'    cursor at {frac:4.0%}: {dt * 1e3:8.3f} ms/page, ~{steps * 100 // 5:>9} VM instructions/page, {len(rows)} rows')
