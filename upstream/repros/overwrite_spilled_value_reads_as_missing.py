"""A key that is never deleted sometimes reads as missing while its large value is being overwritten.

One process overwrites a single key with large (file-backed) values; reader processes get() it with
a sentinel default. The key exists the whole time, yet some reads return the default.
"""

import multiprocessing
import os
import tempfile

import diskcache

OVERWRITES = 3000
READERS = 6
MISSING = 'MISSING'


def writer(directory: str, start) -> None:
    with diskcache.Cache(directory) as cache:
        start.wait()
        for n in range(OVERWRITES):
            cache.set('hot', n.to_bytes(4, 'big') + os.urandom(150_000))


def reader(directory: str, start, done, results) -> None:
    misses = reads = 0
    with diskcache.Cache(directory) as cache:
        start.wait()
        while not done.is_set():
            reads += 1
            misses += cache.get('hot', MISSING) == MISSING
    results.put((reads, misses))


if __name__ == '__main__':
    ctx = multiprocessing.get_context('spawn')
    with tempfile.TemporaryDirectory() as tmp:
        with diskcache.Cache(tmp) as cache:
            cache.set('hot', os.urandom(150_000))
        start, done, results = ctx.Barrier(READERS + 1), ctx.Event(), ctx.Queue()
        readers = [ctx.Process(target=reader, args=(tmp, start, done, results)) for _ in range(READERS)]
        w = ctx.Process(target=writer, args=(tmp, start))
        for p in [*readers, w]:
            p.start()
        w.join()
        done.set()
        counts = [results.get() for _ in readers]
        for p in readers:
            p.join()
        reads, misses = sum(r for r, _ in counts), sum(m for _, m in counts)
        print(f'{reads} reads of a key that was never deleted, {misses} returned the default')
        bug = misses > 0
        print('BUG REPRODUCED' if bug else 'not reproduced')
        raise SystemExit(1 if bug else 0)
