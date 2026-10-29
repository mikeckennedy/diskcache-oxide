# diskcache-oxide vision

Ideas that aren't ready to plan: worth remembering, not committed to. When one becomes real work, it moves into a phase in [00-roadmap.md](00-roadmap.md) and gets its own plan.

## Expiry that keeps itself accurate (Michael, 2026-10-28)

**Today**, as in diskcache:
- Lookups are already exact: from the moment an entry expires, `get`, `in`, `pop`, `add` and `incr` treat it as missing.
- What lags is everything that counts or lists rows. `len()` counts expired rows, `__iter__` and `iterkeys()` yield them, and `volume()` (so `size_limit` culling) includes their bytes.
- Those rows are physically removed only by the small sweep each `set` does (up to `cull_limit` rows) or an explicit `expire()`. A cache that's mostly read, or a burst of entries that expire together, can leave `len()` and the listings wrong for a long time.

**The idea:** expire on a timer, so "expires at noon" means gone at noon everywhere, not just for lookups. Pieces, roughly in the order they'd be worth doing:

1. **Filter listings at query time:** `__iter__`/`iterkeys()` skip rows with `expire_time <= now`. It's cheap (the keyset pages already run a query) and exact, with no thread. It diverges from diskcache, so it would need a divergence entry.
2. **A background sweeper per store** (also the home of WAL checkpointing, Phase 2.5 item 10 in the roadmap, so this thread is now planned rather than only an idea): a Rust thread, so no Python and no interpreter involvement, that runs the existing chunked `expire()` every interval (say once a minute; configurable, off-able).
   - It first probes for any expired row (a cheap read), so an idle or tidy cache never takes the write lock.
   - Chunks yield to waiting writers, as `clear()` does today.
   - Processes coordinate through a "next sweep at" time in the `totals` row, updated in the sweep's own transaction, so eight gunicorn workers don't all sweep.
   - The thread holds only a `Weak` reference to the store and exits when the store retires.
   - After `fork()` the child has no thread, so it starts one lazily on first use, keyed on the process generation like the pool.
3. **`len()` accuracy:** with the sweeper, `len()` stays within one interval of exact and keeps its O(1) `totals` read. An exact `len()` would need a filtered `COUNT(*)`, so it's probably an opt-in method rather than the default.

**Refinement (Michael): hide expired rows everywhere, and make removal pure housekeeping.** If every query hides expired rows, nothing depends on when they're physically removed. The sweep then only reclaims space (rows and value files), so it can run rarely and off the hot path.
- `len()` stays exact and cheap: `totals.count` minus the expired-but-unswept rows. The partial `cache_expire` index reaches those directly (`expire_time IS NOT NULL AND expire_time <= now`), so the cost is proportional to the backlog, which the sweeper keeps small.
- `volume()` keeps counting their bytes: it measures disk use, and they're still on disk until swept.
- **No sweep on open (decided):** the background sweeper (2) is the one place cleanup happens. Since queries hide expired rows, its timing never affects correctness. This keeps D-019 (opening never writes) unchanged and startup free of extra work. A sweep on open (probe first, `write_nowait`, a 10-20 ms budget) was considered and dropped.

- **Rewriting an expired key (Michael):** it must overwrite the expired row and make it live again, never store a second copy. This already holds: the key is unique, so `set`/`add` over an expired key is an upsert of the same row, with a new value, expiry and `store_seq`. The sweeper must keep it safe: it deletes with `expire_time <= now` checked inside its own transaction, so a row rewritten just before (no longer expired) never matches, and a sweep can't remove a fresh value. Add a test that races the sweeper against overwrites of expiring keys.

**A thread-free alternative to (2):** piggyback the same "is a sweep due?" check on ordinary operations, using `write_nowait`, so it never blocks a caller. It's simpler and naturally fork-safe, but a cache nobody touches never sweeps (its disk space stays used).

**Costs to weigh:** a periodic write transaction on busy caches, one more thread per cache directory, and divergences from diskcache's "lazy until asked" behavior. Size the interval against the write cost measured in Phase 2 (about 20 µs per write transaction, plus about 100 rows per chunk).

## In-process near-cache

Not planned; recorded so it isn't re-derived (Michael, 2026-10-28: "interesting, but a little fraught"). The idea is a per-process memory layer that `get` checks before SQLite. Each value's encoded bytes are stored with a generation counter from a shared-memory file of per-key-hash counters that writers bump after each commit. A hit skips SQLite (and its `-shm` lock) entirely. Writes stay synchronous.

- **Why it's on hold:** an `mmap`'d counter file and its cross-process memory ordering are hard to get right, and harder across platforms (Windows is on the roadmap).
- **Rejected variant, buffering writes in memory and flushing them periodically:** it breaks cross-process `add()`/`incr()` atomicity (the rate limiter), makes writes invisible to other processes until the flush, loses them on a crash, and duplicates them across `fork()`.


## A large-file cache

Pure speculation (Michael, 2026-10-28): "a complicated feature, but it also might be neat". A sibling of `Cache`, as `NullCache` is, for caching whole files: rendered images, downloads, generated PDFs, model outputs. Each file is stored **unmodified** in a files folder inside the cache directory (compression is possible, but probably not), and the database holds only its key and metadata: size, store and access times, expiry, content type, perhaps a checksum. Everything the cache already does for keys then manages real files: `expire`, `cull` and `size_limit`, eviction, `delete`, the cross-process write lock, `add()` atomicity.

- **What it would add over spilled values:** large values already go to value files, but encoded and readable only through the cache. Unmodified files could be handed out by path, so a web app could serve them directly (`sendfile`, the web server's `X-Accel-Redirect`/`X-Sendfile`, streaming) without reading them into Python. They could also be added by moving a file in instead of copying its bytes through Python.
- **The hard parts:** a reader holding a path while the file is evicted, overwritten or culled (hand out an open file, not a path, or rely on POSIX unlink-while-open and write to a temp name then rename); writing a large file atomically, then committing its row, without orphans after a crash (the spill protocol's temp-then-rename, and `check()`'s orphan scan, cover the shape); quota accounting that counts files, not pages; and what "unmodified" means for metadata like the original file name or `mtime`.
- **API sketch, not decided:** `set_file(key, path_or_fileobj, *, move=False, expire=None)`, `get_path(key)` or `open(key)` (returns a read-only file object), `get_file(key, dest)`, plus everything `Cache` has for keys.
