import sqlite3, sys, tempfile, threading, time, random, os
tmp = sys.argv[1]
def make_db():
    path = os.path.join(tempfile.mkdtemp(dir=tmp), 'cache.db')
    c = sqlite3.connect(path); c.execute('PRAGMA journal_mode=WAL'); c.execute('CREATE TABLE t (key TEXT PRIMARY KEY, value TEXT)')
    c.executemany('INSERT INTO t VALUES (?, ?)', [(f'k{i:05d}', f'{i:06x}') for i in range(700)]); c.commit(); c.close()
    return path
def run(paths, threads, seconds=3.0):
    counts = [0] * threads; stop = time.monotonic() + seconds
    def w(i):
        c = sqlite3.connect(paths[i % len(paths)], check_same_thread=False, isolation_level=None)
        q = 'SELECT value FROM t WHERE key = ?'; r = random.Random(i)
        keys = [f'k{j:05d}' for j in r.choices(range(700), k=5000)]; n = 0
        while time.monotonic() < stop:
            for k in keys: c.execute(q, (k,)).fetchone()
            n += len(keys)
        counts[i] = n
    ts = [threading.Thread(target=w, args=(i,)) for i in range(threads)]
    s = time.monotonic(); [t.start() for t in ts]; [t.join() for t in ts]
    return sum(counts) / (time.monotonic() - s) / 1000
shared = make_db(); own = [make_db() for _ in range(4)]
print(sys.version.split()[0], 'GIL', sys._is_gil_enabled(), 'sqlite', sqlite3.sqlite_version)
print(f'1 thread:                    {run([shared], 1):6.0f}k selects/s')
print(f'4 threads, one db file:      {run([shared], 4):6.0f}k selects/s')
print(f'4 threads, one file each:    {run(own, 4):6.0f}k selects/s')
