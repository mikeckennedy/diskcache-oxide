import sys, tempfile, threading, time, random
import diskcache_oxide as d
tmp = sys.argv[1]
N = 60_000
def fill(dirpath):
    c = d.Cache(dirpath, timeout=1.0, durable=True)
    for i in range(700): c.set(f'https://talkpython.fm/static/img/{i:05d}/cover.webp?{i}', f'{i:06x}')
    return c
def keys_for(seed):
    r = random.Random(seed); return [f'https://talkpython.fm/static/img/{i:05d}/cover.webp?{i}' for i in r.choices(range(700), k=N)]
def run(caches, threads, **kw):
    lists = [keys_for(t) for t in range(threads)]
    bar = threading.Barrier(threads + 1)
    def w(i):
        get = caches[i % len(caches)].get
        ks = lists[i]; bar.wait()
        for k in ks: get(k)
    ts = [threading.Thread(target=w, args=(i,)) for i in range(threads)]
    [t.start() for t in ts]; bar.wait(); s = time.perf_counter()
    [t.join() for t in ts]
    return threads * N / (time.perf_counter() - s) / 1000
shared_dir = tempfile.mkdtemp(dir=tmp); shared = fill(shared_dir)
own = [fill(tempfile.mkdtemp(dir=tmp)) for _ in range(4)]
big = d.Cache(tempfile.mkdtemp(dir=tmp), timeout=1.0, durable=True, max_connections=32)
for i in range(700): big.set(f'https://talkpython.fm/static/img/{i:05d}/cover.webp?{i}', f'{i:06x}')
print('GIL enabled:', sys._is_gil_enabled())
for _ in range(2):
    print(f"1 thread, one cache:              {run([shared], 1):7.1f}k gets/s")
    print(f"4 threads, one shared cache:      {run([shared], 4):7.1f}k gets/s")
    print(f"4 threads, one cache each (dirs): {run(own, 4):7.1f}k gets/s")
    print(f"4 threads, shared, 32 conns:      {run([big], 4):7.1f}k gets/s")
