"""clear() also deletes entries written while it is running, so its work grows with the write rate.

A clear() that starts with 50,000 entries removes those plus every entry another writer adds while
it runs. Writes that callers saw succeed are silently lost.
"""

import tempfile
import threading
import time

import diskcache

with tempfile.TemporaryDirectory() as tmp:
    cache = diskcache.Cache(tmp)
    for i in range(50_000):
        cache.set(f'old{i}', i)

    stop = threading.Event()
    written: list[str] = []

    def writer() -> None:
        with diskcache.Cache(tmp, timeout=1) as w:
            while not stop.is_set():
                key = f'new{len(written)}'
                w.set(key, 1)
                written.append(key)

    t = threading.Thread(target=writer)
    t.start()
    time.sleep(0.2)
    before = len(written)
    removed = cache.clear()
    stop.set()
    t.join()

    during = written[before:]
    survivors = sum(1 for k in during if k in cache)
    print(f'entries when clear() started: ~{50_000 + before:,} | clear() removed: {removed:,}')
    print(f'successful writes while clear() ran: {len(during):,} | still present afterwards: {survivors:,}')
    bug = removed > 50_000 + before
    print('BUG REPRODUCED (clear() deleted writes made after it started)' if bug else 'not reproduced')
    raise SystemExit(1 if bug else 0)
