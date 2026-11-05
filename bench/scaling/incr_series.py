import sys, tempfile, os, time, random
backend, tmp = sys.argv[1], sys.argv[2]
d = tempfile.mkdtemp(dir=tmp)
if backend == 'stock':
    import diskcache; c = diskcache.Cache(d, timeout=0.25); wal = os.path.join(d, 'cache.db-wal')
else:
    import diskcache_oxide; kw = {} if len(sys.argv) < 4 else {'wal_size_limit': int(sys.argv[3])}
    c = diskcache_oxide.Cache(d, timeout=0.25, durable=True, **kw); wal = os.path.join(d, 'diskcache-oxide.sqlite-wal')
keys = [f'rl:h:10.0.{i // 256}.{i % 256}:2026110512' for i in range(3000)]
for k in keys: c.add(k, 0, expire=3600)
picks = random.Random(1).choices(keys, k=4000)
out = []
for batch in range(12):
    s = time.perf_counter()
    for k in picks: c.incr(k, default=None)
    out.append(((time.perf_counter() - s) / len(picks) * 1e6, os.path.getsize(wal) // (1024 * 1024)))
label = backend + ('' if len(sys.argv) < 4 else f' wal_size_limit={int(sys.argv[3])//2**20}MiB')
print(f'{label:34}', ' '.join(f'{us:4.1f}µs@{mb}M' for us, mb in out))
