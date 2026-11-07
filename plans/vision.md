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

**Built (2026-10-30), see `plans/08-phase-3-prep-caches-and-self-healing.md`:** `Cache(..., on_decode_error='raise' | 'miss' | callable)` and the `Caches` registry, with per-call `timeout=` on `clear()`/`expire()`/`cull()`. The design below is the reasoning; the plan records what changed on the way (classification at the source, `ForeignValueError`, a 10 s open retry).

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
general = caches['general']  # opened on first use, then reused
caches.disable({'show_pages'})  # NullCache(invalidate=real)
caches.stats()
caches.clear('general')
caches.close_all()
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

## A diagnostic trace for production hangs

Michael, 2026-10-30, after the architect found a hang that needed a fork, a held transaction and a GC finalizer on another thread at once: such hangs are very hard to debug from outside. Give users a switch that records everything the concurrency core does, safe to send to us.

- **Turning it on:** an environment variable, e.g. `DISKCACHE_OXIDE_TRACE=/tmp/dco-trace`, so it works without code changes (gunicorn, granian, a cron job), plus `diskcache_oxide.trace(path)` for code. Off by default and costing one relaxed atomic load per event site when off.
- **What it records:** timestamped events per thread and process: gate entries and exits with depth, fork phases (claim, waits for levels, the before-fork closes, parent and child resets), write-lock acquire, wait, starve and release, connection-pool checkout and checkin, slot waits, held-transaction begin, lend, savepoint, commit, rollback and abandon, value-file creates and unlinks, checkpoints, and every wait longer than a threshold, with what it waited for and who held it.
- **Private by design:** no keys, values or file contents. Directories and keys appear only as short salted hashes (so events on one cache still correlate), with thread ids, pids, generations and timings. A user can read it before sending it.
- **Catching a hang in the act:** a bounded in-memory ring buffer per process, always written to disk when a wait passes a deadline (say 10 s), or on a signal (`SIGUSR2`, as the soak's stall dumps do), together with a snapshot of the state: gate counts and pending flag, parked levels, each store's write-lock word, waiters and starving counts, pinned slot state, pool counts. That snapshot alone would have shown this hang: a pending fork, one held level, and its ender waiting at depth 0.
- **Fork-safe output:** one file per pid (a child starts its own after its reset), written with plain `write` calls, never a lock the child could inherit held.
- **A reader:** `python -m diskcache_oxide.trace report <file>` merges per-process files and prints the wait-for graph at the moment of the hang.
- **Open questions:** the `tracing` crate against a hand-rolled ring buffer (it must not allocate or lock on the hot path), the overhead budget when on (aim for usable in production for an hour), and whether the snapshot is also worth exposing on its own as `Cache._debug_state()` for the soak.

## A cache server for every language (Michael, 2026-11-06)

**Very speculative, and a separate product** (Michael, 2026-11-06): its own name, docs and pitch on the same engine, not a feature of diskcache-oxide, whose appeal stays "no server to run". Nothing is planned. It's recorded so the shape isn't re-derived, and so work on the library keeps it possible ("Keeping the door open", below). The idea: a self-contained Rust binary on `diskcache-oxide-core` (no PyO3, no Python), shipped as a Docker image, serving named caches over HTTP and later a binary protocol, with thin clients for Python, Rust, .NET, TypeScript and Java. It speaks our own API, not RESP and not Redis's command set: it isn't meant to be a Redis clone.

**Why, in order of weight:**
- **One cache for the machine.** Talk Python, Python Bytes, Training and InterviewCue each map their own cache volumes and think about them separately at deploy time. One container serves them all, and each app reaches it by hostname.
- **Memory.** Today every worker process of every app pays for its own SQLite page caches: about 8 MiB per `Cache` and 16 MiB per 8-shard FanoutCache (D-023, D-037), plus up to about 48 fds per FanoutCache. With 8 apps, 4 workers each and two or three FanoutCaches per app, that's roughly 1-1.5 GiB of mostly duplicated hot pages. The server holds one copy, and can afford a much larger page cache (say 128 MiB in total), which also raises hit rates on wide, uniform reads. Caveat: the cached data itself isn't duplicated today, since the OS page cache holds a file once for every process; the saving is in SQLite's private caches, fds and WAL growth. Measure it before quoting it (open questions).
- **The WAL stays small.** D-024's WAL growth (checkpoints failing at 1%, WALs reaching 6-21 GiB) comes from readers in other processes pinning old snapshots. A server that's the files' only owner, with `locking_mode=EXCLUSIVE`, has no such readers and checkpoints whenever it likes.
- **One disk budget.** Each app now sizes its own `size_limit` for its own peak, and the headroom adds up. A server could hold one budget, with caps or floors per namespace.
- **Every language shares one cache.** Python first, but .NET, Node, Java and Rust apps can read and write the same entries. That's new: today the technology is Python-only.
- **A replacement for Redis or Valkey as a plain cache:** durable (it restarts warm, unlike Redis without persistence), bigger than RAM, much less memory, and almost nothing to configure.

**Performance, honestly.** The server can't beat the in-process library, which reads in about 1 µs; a loopback round trip alone costs more. The claim to defend is Redis-class latency (both pay the same network cost, and our SQLite read is sub-microsecond), better throughput across cores than Redis's single-threaded command loop, and a fraction of the memory. Benchmark against Redis and Valkey on the same box, and lead with memory, durability and dataset size, not latency.

**Storage:**
- One FanoutCache per namespace, with its shard count fixed at creation (default 8; plan 15 already stores it on disk).
- `locking_mode=EXCLUSIVE`, per the roadmap's measurement (0.69 µs reads against WAL's 1.18 µs). To verify: with EXCLUSIVE and WAL, the WAL index lives in the connection's heap, so only one connection can use each file, even in one process. If so, one thread per shard owns that shard's connection and requests are routed to it by key hash. At 0.69 µs a read, one shard thread outruns anything HTTP can send it.
- Group commit: the shard thread gathers concurrent writes into one transaction, since most of a write's cost is the commit.

**Namespaces, one per app** (decided): each its own FanoutCache with its own `size_limit`, eviction, stats and `clear`, and optionally its own auth token. Not a key prefix in one shared cache, where one busy app would evict the others' entries. Apps that cache the same data can share a namespace on purpose.

**Data model, shared across languages:**
- Keys are UTF-8 strings (perhaps raw bytes too), so `user:42` means the same key in every language. The local `Cache` keeps its Python key model; the remote one has its own, simpler model.
- Values are bytes plus a format tag: `msgpack`, `json`, `bytes`, `text`, or `python` (pickle and `register_type` objects only Python can read). The server never decodes values; each client decodes what it understands and raises a clear error otherwise. msgpack is the shared format.

**Operations:** `get`, `set`, `delete`, `add`, `incr`, `pop`, `expire`/`touch`, tags and `evict(tag)`, `get_many`/`set_many`/`delete_many`, and an atomic batch. No `transact()` over the wire: holding a shard's write lock across round trips lets one stalled client stall the shard, so atomic server-side operations take its place. Admin endpoints: `stats`, `volume`, `cull`, `check`, health, and backup through SQLite's online backup API.

**Transports:** one Rust operation enum, with each transport a thin adapter into and out of it.
- HTTP with msgpack and HTTP with JSON on the same endpoints, chosen by `Content-Type`. JSON means any language works with curl and no client at all.
- Later, a binary protocol over TCP and Unix sockets: frames with a length prefix and a request ID, so many requests can be in flight on one connection. That's where we beat HTTP, mostly through pipelining. Build it when benchmarks show the gap, without a rewrite. Reliable transports only, no UDP.
- Large values: stream spilled value files with `sendfile`.

**Deployment:** a Docker image with a restart policy, bound to the Docker network, bearer tokens per namespace, TLS at Caddy or NGINX when traffic leaves the host. The server is a binary, so configuration through a file and environment variables is fine there; the library's no-environment-variables rule stays a library rule. On SIGTERM it signals clients that it's going away (`Connection: close`, HTTP/2 GOAWAY, or a frame), drains requests in flight, checkpoints and exits. Durable storage means a restart comes back warm.

**Clients:**
- **Python first:** a `RemoteCache` that implements `CacheLike`, written in Rust inside `_core`, so there are no new Python dependencies and the connection pool reuses the fork-reset machinery we've already proven. The `Caches` registry decides whether a purpose is local or remote, so app code doesn't change.
- **Then Rust** (nearly free), then .NET (C#) and TypeScript/Node, and Java when someone asks. Native and thin (a few hundred lines each, no native binaries, which .NET and Node developers avoid), rather than one Rust client wrapped through FFI everywhere.
- **Framework adapters, where developers actually plug a cache in** (Michael, 2026-11-06): a Django cache backend (the remote twin of the Django backend above) and ASP.NET Core's `IDistributedCache` for .NET first, then the equivalents elsewhere (keyv for Node, Rails' `ActiveSupport::Cache::Store`, Spring's `Cache`) as demand shows. Each sits on that language's native client, so it inherits the failure model below. These are how we answer Redis's ecosystem: every framework ships a Redis backend, and an adapter for the few interfaces that matter most reaches most of those users.
- **One shared test suite for clients:** a language-neutral spec plus test vectors (requests, expected responses, byte-level frames) that every client runs, like our conformance suite but aimed at the wire.

**Failure model: unreachable means NullCache** (decided, to keep the story simple). A single server for the machine is a single point of failure for every app, so every client must hold to this:
- Each call has a short timeout (50-100 ms). `get`, `set`, `delete` and `touch` get one quick retry on a connection error, then that call misses and the request moves on.
- Reconnecting happens in the background with backoff (100 ms, 250 ms, 500 ms, 1 s...), so requests never pay for it.
- A 5-second grace window: only 5 s of continuous failure trips the breaker and logs one WARNING (and one on recovery, never one per call), so a container restart that finishes in 2 s makes no noise. "Connection refused" (port closed, usually a restart) is cheap to retry; timeouts (a hung server or a bad network) cost the full timeout each time, so they trip sooner and retry more gently. A planned restart's going-away signal puts clients straight into the grace window, logged at INFO.
- While tripped, reads miss and writes are dropped. Coordination operations (`add`, `incr`, `pop`, locks, throttle) raise instead, because two workers each "acquiring" a lock against NullCache means the lock has silently stopped working. `incr`, `add` and `pop` are never retried, since a reply lost after the server applied them would double them.
- A `delete` made during an outage is lost, so the server may serve the old value until it expires. The docs recommend an `expire` on everything stored remotely, and the client logs each dropped delete at WARNING with its key (deduped), so an operator can see what to clear.

**Considered and set aside** (eventual consistency is hard to explain, and the pitch is simple): a local fallback cache in each client (a local `Cache` for Python, a memory LRU elsewhere); replaying the outage's deletes, `evict(tag)` and `clear` on reconnect; making them conditional on the row's `store_time` ("delete if not stored since T", with the client converting T to the server's clock using an offset learned from each reply); deleting only if the row's version is still the one the client saw; and request IDs that let the server spot a retried `incr`. Also set aside: a RESP (Redis protocol) compatibility subset, which would let existing Redis backends and tools (redis-cli, RedisInsight) connect unchanged. It would buy reach, but means chasing Redis's semantics; framework adapters on our own API are the chosen route (Michael, 2026-11-06). Kept for its own sake: **compare-and-set** for writers racing on one key ("only if nobody changed it since I read it"). `store_seq` already gives every write a version that only goes up, per shard, so it needs no schema change.

**Open questions:**
- Measure the memory claim first: `bench/memory.py` per process today, then the real total for the Talk Python apps on htz against one server container.
- Confirm that EXCLUSIVE with WAL means one connection per file, and measure one thread per shard against a small pool in plain WAL mode.
- When a write is acknowledged: after its commit (durable), or on group commit's schedule (fast), or chosen per call.
- Keys: strings only, or bytes too.
- A disk budget for the whole machine, or only a `size_limit` per namespace.
- Where it lives: `crates/diskcache-oxide-server` in this workspace, and whether each language's client gets its own repository.
- Auth beyond a bearer token per namespace, and the server's name.

**Keeping the door open while building the library.** No work on the library waits on the server, but these choices keep it cheap to build later:
- `diskcache-oxide-core` stays free of PyO3 and Python types, dealing only in keys, modes, bytes and value files, and nothing in it should assume a Python host process.
- Every Python-specific encoding (key types, msgpack, JSON and pickle of Python objects, `register_type`) stays in the extension's `codec/`, never in core.
- Rows keep `store_time` and a `store_seq` that only goes up per database: the server's compare-and-set depends on it.
- Single-key operations that are atomic in one call (`add`, `incr`, `pop`, compare-and-delete) stay atomic without `transact()`, since the server can't offer held transactions.
- When it's cheap, SQLite settings stay core open options rather than hard-coded: the server wants `locking_mode=EXCLUSIVE` and a much larger page cache than the library's per-process budget.
