"""fcntl calls per `get`, threads in one process: a plain Cache against a FanoutCache(8) (plan 34, step 2).

Usage, under strace in a Linux container (mechanism only; strace slows the threads, so no timing is meaningful):
    strace -f -c -e trace=fcntl,fstat,pread64 python bench/scaling/fcntl_per_get.py cache 2 20000
    strace -f -c -e trace=fcntl,fstat,pread64 python bench/scaling/fcntl_per_get.py cache 2 0   # setup only

Arguments: `cache` or `fanout`, the thread count, and gets per thread. Subtract the setup-only count, then divide
by the gets. SQLite counts shared -shm locks per process and calls fcntl only when a count leaves or returns to zero,
so threads that overlap on one file skip the calls; sharded over 8 files they rarely overlap.
"""

import random
import sys
import tempfile
import threading
import time

import diskcache_oxide

variant, threads, per_thread = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
root = tempfile.mkdtemp()
cache = diskcache_oxide.FanoutCache(root, shards=8) if variant == 'fanout' else diskcache_oxide.Cache(root)
for i in range(1_000):
    cache.set(f't:{i}', 'v' * 1_024)
cache.checkpoint()
keys = [f't:{i}' for i in range(1_000)]
for key in keys:
    cache.get(key)
barrier = threading.Barrier(threads + 1)


def worker(seed: int) -> None:
    rng = random.Random(seed)
    plan = [keys[rng.randrange(1_000)] for _ in range(per_thread)]
    get = cache.get
    barrier.wait()
    for key in plan:
        get(key)


pool = [threading.Thread(target=worker, args=(n,)) for n in range(threads)]
for thread in pool:
    thread.start()
time.sleep(0.2)
barrier.wait()
for thread in pool:
    thread.join()
print('gets', threads * per_thread)
