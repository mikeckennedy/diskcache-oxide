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

## A Django cache backend

Michael, 2026-10-29: plug diskcache-oxide straight into Django's cache framework (https://docs.djangoproject.com/en/6.1/topics/cache/, the `CACHES` setting and `BaseCache` backends; the brand-new part of Django 6 is the separate Tasks framework, not the cache). Upstream diskcache already ships a prototype of this (`diskcache.DjangoCache`, a `FanoutCache` behind Django's `BaseCache`), so it's both a compatibility item for people switching and a chance to do better. Which Django versions to support is still open.

- **Shape:** a `diskcache_oxide.django.DiskcacheOxideCache` backend for `CACHES = {"default": {"BACKEND": ..., "LOCATION": "/var/cache/app", "OPTIONS": {...}}}`, mapping Django's API (`get`/`set`/`add`/`delete`/`touch`/`incr`/`decr`/`get_many`/`set_many`/`delete_many`/`clear`/`has_key`, `timeout=None` meaning forever and `0` meaning don't cache, key prefixes, versions and `KEY_FUNCTION`) onto the cache's own calls. Django's async methods (`aget`, `aset` and the rest) would run the sync calls in a thread, as Django's own default does, unless a native async path is worth it.
- **Why it fits:** Django apps run under gunicorn or uvicorn workers, many processes on one host, exactly the cross-process, fork-safe workload the soak tests. `add()` and `incr()` are atomic across processes, which Django's file-based cache can't promise, and there's no server to run, unlike Redis or Memcached.
- **Depends on:** FanoutCache (6c), since Django sites are threaded and write from many workers; `*_many` as single transactions; and Django's test suite for cache backends as the conformance oracle, the way diskcache 5.6.3 is for `Cache`.
- **Open questions:** a separate optional extra (`diskcache-oxide[django]`) or a module that imports Django lazily; how `pickle` interacts with D-001 (Django caches arbitrary objects, so the backend would enable pickle); and whether to match `diskcache.DjangoCache`'s extra methods (`memoize`, `cache` access) for drop-in switching.

## DurableCache and the cache service, in the library

Michael, 2026-10-30: Talk Python's `DurableCache` wrapper and its `cache_service` factory might belong in diskcache-oxide, the way `NullCache` does. **Decided (Michael, 2026-10-30):** both come into the library. Self-healing reads become a `Cache` constructor option, and the registry is named `caches` (not `cache_service`, which is Talk Python's house naming). The details get settled in Phase 3 planning; nothing is built yet. Evidence that it's generic: `durable_cache.py` is copied into four apps (Talk Python, Training, Python Bytes, InterviewCue), and the copies have drifted apart (136 to 349 lines).

**What `DurableCache` does today, and where each piece lands:**
- `bool()` always True, fixing diskcache's empty-cache-is-falsy: already in the library (D-009).
- The `CacheProtocol` Protocol: already `CacheLike`.
- `NullCache` and `PerfTestNullCache`: already `NullCache` and `NullCache(invalidate=)`. The construction-time warning and Sentry report stay in the app.
- `name`, for logs and the admin page: trivial, and belongs with the registry below.
- **The one real feature, a self-healing `get`:** any exception while reading (in practice an unpickle that fails after a module move) is logged, the entry is deleted, and the default is returned, so a cache can never crash a request.

**Self-healing reads as a `Cache` option, not a wrapper class.** A wrapper adds a Python frame to every call, which Phase 1 ruled out, so this should be a constructor option, something like `Cache(dir, on_decode_error="miss")` (or `"raise"`, the default). Doing it inside the library is better than the wrapper in ways the wrapper can't match:
- **Delete only what failed.** The wrapper's `get`-fails-then-`delete` can remove a fresh value a peer wrote in between. The library can compare-and-delete on `store_seq`, as the spill protocol already does.
- **Classify the error.** `CorruptValueError` and an unpickle failure (`ModuleNotFoundError`, `AttributeError`) are safe to treat as a miss and remove. `UnsafeValueError` (a pickle row read by a `pickle=False` handle) must be a miss that **leaves the row**, since D-001 says a handle never destroys a peer's data.
- **Don't hide timeouts by accident.** The wrapper catches `Exception`, so today a busy-lock `Timeout` on `get` is also a silent miss, followed by a `delete` that will usually time out too. If misses-on-timeout are wanted (they suit Talk Python, where every caller falls back to its source of truth), that should be a separate, explicit choice.
- **Reporting:** a stdlib `logging` logger, plus an optional `on_error(key, exc)` callback the app wires to Sentry, called after the connection is released.
- It matters less with oxide: JSON and msgpack are the default codecs, so module moves only affect the caches that opt into `pickle=True`.

**The cache service: a small registry of named caches.**

The parts of `cache_service.py` that any app needs:
- name to directory under one base dir, with an env-var override for tests;
- per-cache settings (`size_limit`, `timeout`);
- opened lazily, once, and memoized;
- a `NullCache` when opening fails, with a reporting hook;
- on/off switches that return `NullCache(invalidate=real)`;
- `stats()` (len and volume per cache, for an admin page), `clear(name)`, `expire_all()` and `close_all()` at shutdown.

The app-specific parts stay in the app: the purpose names and their `Literal`, the mapping from purposes onto physical caches, reading the settings file, Sentry and logbook, cache descriptions, and the MP3 and HTTP helpers.

A sketch of the API (the name `caches` is decided; whether it's a module, `diskcache_oxide.caches`, or a class is not):

```python
caches = Caches('/var/cache/app', timeout=1.0, on_open_error=report)
caches.register('general')
caches.register('show_pages', size_limit=2 * GiB)
general = caches['general']            # opened on first use, then reused
caches.disable({'show_pages'})         # NullCache(invalidate=real)
caches.stats(); caches.clear('general'); caches.close_all()
```

Why the library is the better home:
- **Thread safety:** the app's module-level dicts aren't safe on free-threaded 3.14t, and a library registry would lock the lazy open.
- **Fork awareness:** the registry can rely on the library's fork handling rather than each app reasoning about it.
- **Fewer moving parts.** The hourly lazy `expire_all()` becomes unnecessary once the background sweeper (above) lands. The separate maintenance timeout becomes a per-call `timeout=` on `clear()`/`expire()`, so the admin path stops reopening a fresh handle for every stats or clear call.

**Guardrails:**
- An instance the app owns, not a module-level singleton, so tests make their own and the current test seams go away.
- Pure Python, small, with no new Rust.
- No settings-file format and no logging framework of its own.
- If it starts to grow features, it stays in the app.

**Timing:** design both in Phase 3 planning and build them in the library first, so Talk Python adopts the library versions directly instead of porting `DurableCache` and then replacing it. The other three apps can then drop their copies too.
