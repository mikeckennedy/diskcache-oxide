# Vision and roadmap


# The vision

A shared cache is one of the first pieces of infrastructure a Python web app grows into, and today the choice is awkward. An in-process dict is fast, but each worker has its own copy and it's gone on restart. Redis is shared and durable, but it's a separate server, a network hop on every read, and one more thing to run. [python-diskcache](https://github.com/grantjenks/python-diskcache) showed there's a lot of room between those two: a cache that is just a directory, shared by every thread and process on the machine, built on SQLite.

diskcache-oxide is a bet that this middle option can be much bigger than it has been. With a Rust engine underneath, a directory-backed cache can be fast enough to call on the event loop, safe enough for pre-forking servers and free-threaded Python, and complete enough to cover the jobs people now reach for Redis to do: caching, counters, rate limits, locks, job claims and queues. And when one machine isn't enough, the same engine can be served over the network.

It's being built in three stages.


## Stage 1: a better diskcache (now)

Keep diskcache's API, fix what's wrong underneath it, and make it fast. Behavior comes first: every phase keeps a conformance suite green against real diskcache 5.6.3 before chasing speed, and every deliberate difference is written down in [Divergences](divergences.md).


## Stage 2: the complete local caching toolkit

Everything diskcache offers that real apps use, plus the things a modern Python stack needs that it never had: an async API, sharding designed for many threads, and coordination recipes that are atomic across every process on the machine.


## Stage 3: a cache server

The same Rust core, served over HTTP to many clients: a Redis-style central cache with SQLite underneath, for when your app outgrows one machine. A server that is the only owner of its files can run SQLite in exclusive locking mode, which keeps the WAL index in heap memory instead of a shared file. Measured on the development machine:

| Storage mode | Read | Write | Shared by several processes? |
|----|----|----|----|
| WAL (the library today) | 1.18 µs | 13.9 µs | Yes |
| WAL with exclusive locking (the server) | 0.69 µs | 16.5 µs | No, one owner |


# Where things stand


## Done: the core [Cache](../reference/Cache.md#diskcache_oxide.Cache)

- [get](../reference/Cache.get.md#diskcache_oxide.Cache.get), [set](../reference/Cache.set.md#diskcache_oxide.Cache.set), [add](../reference/Cache.add.md#diskcache_oxide.Cache.add), [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete), [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop), [touch](../reference/Cache.touch.md#diskcache_oxide.Cache.touch), [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr), [decr](../reference/Cache.decr.md#diskcache_oxide.Cache.decr), with expiry.
- The dictionary interface: `cache[k]`, `k in cache`, `del cache[k]`, `len(cache)`, iteration.
- Indexed prefix scans: `iterkeys(prefix=..., reverse=...)`.
- Maintenance: [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire), [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull), [volume()](../reference/Cache.volume.md#diskcache_oxide.Cache.volume), and [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) for the write-ahead log.
- Least-recently-stored eviction with [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) and [cull_limit](../reference/Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit).
- Large values spilled to files, with readers that never see a spurious miss during an overwrite.
- msgpack (default) and JSON serializers with exact type tags; pickle only when you opt in.
- Thread-, process- and fork-safe, on GIL and free-threaded builds of 3.14 and 3.15.
- Bounded connections and memory, and a target size for the write-ahead log.
- [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache), the [CacheLike](../reference/CacheLike.md#diskcache_oxide.CacheLike) protocol, `py.typed`.
- [`transact()`](transactions.md), ahead of the rest of Phase 6, with [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy) and the [TransactionalCacheLike](../reference/TransactionalCacheLike.md#diskcache_oxide.TransactionalCacheLike) protocol.
- [`FanoutCache`](fanoutcache.md) (Phase 6c), sharding across several databases, with `Caches.register(name, shards=...)` and [ShardedCacheLike](../reference/ShardedCacheLike.md#diskcache_oxide.ShardedCacheLike). It removes the multi-process write-lock ceiling; making threads that share one handle scale on free-threaded Python is part of Phase 4.


## Done: concurrency review and soak (Phase 2.5)

The concurrency core is where serious bugs hide, so it got its own phase before anyone runs it in production: independent deep reviews by several AI coding agents, model-checker tests for the lock state machines, and day-long soak runs with many processes and threads, forks, SIGKILLs and [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) under load, checking for corruption, lost writes, leaks and latency drift. The 24-hour soak passed, and it paid for itself along the way: it found the unbounded WAL growth that `wal_size_limit` and [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) now rein in. The limit is a target, not a bound: several processes writing flat out can still take the log well past it (see [Storage and WAL](storage-and-wal.md)).


## Now: the last gates before production (Phase 3)

The version in the repository is `0.1.0.dev2`. What's left before Talk Python runs it in production and `0.1.0` goes to PyPI: a sanitizer pass over the whole codebase (ThreadSanitizer, AddressSanitizer and Miri, with the bundled SQLite built under them), and a fresh 24-hour soak on the final commit, since the performance work since the last one changed [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr), held transactions and locking.


## Next

| Phase | What it delivers |
|----|----|
| **3. Production** | diskcache-oxide replaces diskcache at [Talk Python](https://talkpython.fm), with production latency watched against the old numbers. `0.1.0` goes to PyPI after that; until then the name there holds a placeholder. |
| **4. Free-threaded performance** | Make throughput scale with threads on 3.14t and 3.15t. Today a cached read spends about 60% of its time in one system call SQLite makes for its shared-memory lock, and that's the first target. |
| **5. `AsyncCache`** | An async API for asyncio frameworks that can't slow down the sync one, including `transact_async()`. |
| **6a. Cache options** | More eviction policies (LRU, LFU, none), tags and `evict(tag)`, statistics, `memoize`, `peek`/`pull`/`push`, `check()`, `retry=`, and non-`str` keys. |
| **6b. [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact)** | Multi-operation transactions. Done early: see [Transactions](transactions.md). |
| **6c. [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache)** | Done: see [FanoutCache](fanoutcache.md). |
| **6d. `Deque` and `Index`** | diskcache's persistent collections. |
| **6e. Recipes** | `Lock`, `RLock`, `BoundedSemaphore`, `throttle`, `barrier`, and stampede-proof `memoize`. |
| **7. Network server** | Stage 3, above. |
| **Windows** | Supported as a development platform once macOS and Linux are solid. |


# Further out

Ideas being considered, not yet committed to:

- **Expiry that's exact everywhere.** Lookups already treat an expired entry as gone at the moment it expires. The plan is to make `len()` and iteration do the same, and move the physical cleanup to a small background sweeper written in Rust, coordinated across processes so eight workers don't all sweep at once.
- **Encryption at rest**, if a real need appears: SQLCipher for the database, authenticated encryption for spill files.


# Non-goals

- Reading existing diskcache directories. diskcache-oxide uses its own on-disk format, so caches start cold when you switch.
- A module named `diskcache` (it would collide with the real package).
- `DjangoCache`.
- Python older than 3.14.
