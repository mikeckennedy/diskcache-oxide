import sys, tempfile, threading, time, random, os, sqlite3
tmp = sys.argv[1]; which = sys.argv[2]
def run(get_for_thread, threads, seconds=3.0):
    counts = [0] * threads; stop = time.monotonic() + seconds
    def w(i):
        get = get_for_thread(i); r = random.Random(i)
        keys = [f'k{j:05d}' for j in r.choices(range(700), k=5000)]; n = 0
        while time.monotonic() < stop:
            for k in keys: get(k)
            n += len(keys)
        counts[i] = n
    ts = [threading.Thread(target=w, args=(i,)) for i in range(threads)]
    s = time.monotonic(); [t.start() for t in ts]; [t.join() for t in ts]
    return sum(counts) / (time.monotonic() - s) / 1000
if which == 'oxide':
    import diskcache_oxide as d
    c = d.Cache(tempfile.mkdtemp(dir=tmp), timeout=1.0, durable=True)
    for i in range(700): c.set(f'k{i:05d}', f'{i:06x}')
    wal = lambda: os.path.getsize(os.path.join(c.directory, 'cache.db-wal')) if os.path.exists(os.path.join(c.directory, 'cache.db-wal')) else 0
    print(f'oxide, WAL {wal()//1024} KiB: 1 thread {run(lambda i: c.get, 1):.0f}k, 4 threads {run(lambda i: c.get, 4):.0f}k')
    print('checkpoint ->', c.checkpoint())
    print(f'oxide, WAL {wal()//1024} KiB: 1 thread {run(lambda i: c.get, 1):.0f}k, 4 threads {run(lambda i: c.get, 4):.0f}k')
else:
    path = os.path.join(tempfile.mkdtemp(dir=tmp), 'cache.db')
    w = sqlite3.connect(path, isolation_level=None); w.execute('PRAGMA journal_mode=WAL'); w.execute('PRAGMA wal_autocheckpoint=0')
    w.execute('CREATE TABLE t (key TEXT PRIMARY KEY, value TEXT)')
    for i in range(700): w.execute('INSERT INTO t VALUES (?, ?)', (f'k{i:05d}', f'{i:06x}'))
    def getter(i):
        conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None); q = 'SELECT value FROM t WHERE key = ?'
        return lambda k: conn.execute(q, (k,)).fetchone()
    print(f'sqlite3, WAL {os.path.getsize(path + "-wal")//1024} KiB: 1 thread {run(getter, 1):.0f}k, 4 threads {run(getter, 4):.0f}k')
    w.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    print(f'sqlite3, WAL {os.path.getsize(path + "-wal")//1024} KiB: 1 thread {run(getter, 1):.0f}k, 4 threads {run(getter, 4):.0f}k')
