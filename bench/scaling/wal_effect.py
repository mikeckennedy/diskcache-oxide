"""Effect of WAL size on read throughput with 1 and 4 threads.

Usage (from the repo root):
    venv/bin/python bench/scaling/wal_effect.py TMPDIR oxide|sqlite3

Scratch directories are created under TMPDIR.
"""

import os
import random
import sqlite3
import sys
import tempfile
import threading
import time

tmp = sys.argv[1]
which = sys.argv[2]


def run(get_for_thread, threads, seconds=3.0):
    counts = [0] * threads
    stop = time.monotonic() + seconds

    def w(i):
        get = get_for_thread(i)
        r = random.Random(i)
        keys = [f'k{j:05d}' for j in r.choices(range(700), k=5000)]
        n = 0
        while time.monotonic() < stop:
            for k in keys:
                get(k)
            n += len(keys)
        counts[i] = n

    ts = [threading.Thread(target=w, args=(i,)) for i in range(threads)]
    s = time.monotonic()
    [t.start() for t in ts]
    [t.join() for t in ts]
    return sum(counts) / (time.monotonic() - s) / 1000


if which == 'oxide':
    import diskcache_oxide as d

    c = d.Cache(tempfile.mkdtemp(dir=tmp), timeout=1.0, durable=True)
    for i in range(700):
        c.set(f'k{i:05d}', f'{i:06x}')

    def wal():
        p = os.path.join(c.directory, 'cache.db-wal')
        return os.path.getsize(p) if os.path.exists(p) else 0

    wal_kib = wal() // 1024
    one, four = run(lambda i: c.get, 1), run(lambda i: c.get, 4)
    print(f'oxide, WAL {wal_kib} KiB: 1 thread {one:.0f}k, 4 threads {four:.0f}k')
    print('checkpoint ->', c.checkpoint())
    wal_kib = wal() // 1024
    one, four = run(lambda i: c.get, 1), run(lambda i: c.get, 4)
    print(f'oxide, WAL {wal_kib} KiB: 1 thread {one:.0f}k, 4 threads {four:.0f}k')
else:
    path = os.path.join(tempfile.mkdtemp(dir=tmp), 'cache.db')
    w = sqlite3.connect(path, isolation_level=None)
    w.execute('PRAGMA journal_mode=WAL')
    w.execute('PRAGMA wal_autocheckpoint=0')
    w.execute('CREATE TABLE t (key TEXT PRIMARY KEY, value TEXT)')
    for i in range(700):
        w.execute('INSERT INTO t VALUES (?, ?)', (f'k{i:05d}', f'{i:06x}'))

    def getter(i):
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        q = 'SELECT value FROM t WHERE key = ?'
        return lambda k: conn.execute(q, (k,)).fetchone()

    wal_kib = os.path.getsize(path + '-wal') // 1024
    print(f'sqlite3, WAL {wal_kib} KiB: 1 thread {run(getter, 1):.0f}k, 4 threads {run(getter, 4):.0f}k')
    w.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    wal_kib = os.path.getsize(path + '-wal') // 1024
    print(f'sqlite3, WAL {wal_kib} KiB: 1 thread {run(getter, 1):.0f}k, 4 threads {run(getter, 4):.0f}k')
