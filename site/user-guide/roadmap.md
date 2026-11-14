# Vision and roadmap


# The vision

A shared cache is one of the first pieces of infrastructure a Python web app grows into, and today the choice is awkward. An in-process dict is fast, but each worker has its own copy and it's gone on restart. Redis is shared and durable, but it's a separate server, a network hop on every read, and one more thing to run. [python-diskcache](https://github.com/grantjenks/python-diskcache) showed there's a lot of room between those two: a cache that is just a directory, shared by every thread and process on the machine, built on SQLite. diskcache itself has had no commits since 2023, and its issues and pull requests go unanswered, so the idea needed a new home.

diskcache-oxide is a bet that this middle option can be much bigger than it has been. With a Rust engine underneath, a directory-backed cache can be fast enough to call on the event loop, safe enough for pre-forking servers and free-threaded Python, and complete enough to cover the jobs people now reach for Redis to do: caching, counters, rate limits, locks, job claims and queues. And when one machine isn't enough, the same engine can be served over the network.

It's being built in three stages.


## Stage 1: a better diskcache (done)

Keep diskcache's API, fix what's wrong underneath it, and make it fast. With 0.2.3, everything from diskcache's [Cache](../reference/Cache.md#diskcache_oxide.Cache) and [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) that real apps use is in. Behavior comes first: every phase keeps a conformance suite green against real diskcache 5.6.3 before chasing speed, and every deliberate difference is written down in [Divergences](divergences.md).


## Stage 2: the complete local caching toolkit (in progress)

The things a modern Python stack needs that diskcache never had. Sharding that doesn't lock every shard for a transaction and coordination recipes that hold across every process are already in. Still to come are a Django cache backend, threads that scale on free-threaded Python, and an async API.


## Stage 3: a cache server

The same Rust core, served over HTTP to many clients: a Redis-style central cache with SQLite underneath, for when your app outgrows one machine. A server that is the only owner of its files can run SQLite in exclusive locking mode, which keeps the WAL index in heap memory instead of a shared file. Measured on the development machine:


| Storage mode | Read | Write | Shared by several processes? |
|----|----|----|----|
| WAL (the library today) | 1.18 µs | 13.9 µs | Yes |
| WAL with exclusive locking (the server) | 0.69 µs | 16.5 µs | No, one owner |


# Where things stand


## Done: the core [Cache](../reference/Cache.md#diskcache_oxide.Cache)

- [get](../reference/Cache.get.md#diskcache_oxide.Cache.get), [set](../reference/Cache.set.md#diskcache_oxide.Cache.set), [add](../reference/Cache.add.md#diskcache_oxide.Cache.add), [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete), [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop), [touch](../reference/Cache.touch.md#diskcache_oxide.Cache.touch), [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr), [decr](../reference/Cache.decr.md#diskcache_oxide.Cache.decr), with expiry.
- The dictionary interface: `cache[k]`, `k in cache`, `del cache[k]`, `len(cache)`, iteration and `reversed()`.
- Indexed prefix scans: `iterkeys(prefix=..., reverse=...)`.
- Maintenance: [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire), [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull), [volume()](../reference/Cache.volume.md#diskcache_oxide.Cache.volume), [check()](../reference/Cache.check.md#diskcache_oxide.Cache.check), and [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) for the write-ahead log.
- Least-recently-stored eviction with [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) and [cull_limit](../reference/Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit), or `eviction_policy='none'` for a cache that must never drop a live entry.
- Large values spilled to files, with readers that never see a spurious miss during an overwrite.
- msgpack (default) and JSON serializers with exact type tags, [register_type()](../reference/register_type.md#diskcache_oxide.register_type) for your own types, and pickle only when you opt in.
- Thread-, process- and fork-safe, on GIL and free-threaded builds of 3.14 and 3.15.
- Bounded connections and memory, and a target size for the write-ahead log.
- [`durable=True`](getting-started.md#a-cache-that-cant-take-a-request-down), [`NullCache`](null-cache.md), the [`Caches`](caches.md) registry, the [CacheLike](../reference/CacheLike.md#diskcache_oxide.CacheLike) protocols and `py.typed`.
- [`transact()`](transactions.md) and [`FanoutCache`](fanoutcache.md).


## Done: concurrency review and soak

The concurrency core is where serious bugs hide, so it got its own phase before anyone ran it in production: independent deep reviews by several AI coding agents, model-checker tests for the lock state machines, and day-long soak runs with many processes and threads, forks, SIGKILLs and [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) under load, checking for corruption, lost writes, leaks and latency drift. The 24-hour soak passed, and it paid for itself along the way: it found the unbounded WAL growth that `wal_size_limit` and [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) now rein in. The limit is a target, not a bound: several processes writing flat out can still take the log well past it (see [Storage and WAL](storage-and-wal.md)).


## Done: the first release

`0.1.2` went to PyPI after a sanitizer pass over the whole codebase (Miri, ThreadSanitizer including a free-threaded CPython built under it, AddressSanitizer and LeakSanitizer, with the bundled SQLite built under them) and a fresh 24-hour soak on the released code. It replaced diskcache at [Talk Python](https://talkpython.fm), [Python Bytes](https://pythonbytes.fm) and [Talk Python Training](https://training.talkpython.fm), where production latency is watched against the old numbers.


## Done: parity with diskcache (0.2.3)

0.2.3 fills in what 0.1.2 left out of diskcache's API:

- Keys of every native type: `bytes`, `int`, `float`, `bool`, `None` and tuples, beside `str`.
- [`memoize`](../recipes/caching-expensive-calls.md#a-caching-decorator-memoize) and `retry=`.
- `reversed()`, `pop(expire_time=True)`, pickling a cache, [reset()](../reference/Cache.reset.md#diskcache_oxide.Cache.reset), per-process [stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats) and [check()](../reference/Cache.check.md#diskcache_oxide.Cache.check).
- The [coordination recipes](coordination.md): [Lock](../reference/Lock.md#diskcache_oxide.Lock), [RLock](../reference/RLock.md#diskcache_oxide.RLock), [BoundedSemaphore](../reference/BoundedSemaphore.md#diskcache_oxide.BoundedSemaphore), [throttle](../reference/throttle.md#diskcache_oxide.throttle), [barrier](../reference/barrier.md#diskcache_oxide.barrier), [Averager](../reference/Averager.md#diskcache_oxide.Averager) and [memoize_stampede](../reference/memoize_stampede.md#diskcache_oxide.memoize_stampede).
- [Tags](../recipes/invalidating-groups.md#option-2-tags) with `evict(tag)`, and `eviction_policy='none'`.
- The queue calls [push](../reference/Cache.push.md#diskcache_oxide.Cache.push), [pull](../reference/Cache.pull.md#diskcache_oxide.Cache.pull), [peek](../reference/Cache.peek.md#diskcache_oxide.Cache.peek) and [peekitem](../reference/Cache.peekitem.md#diskcache_oxide.Cache.peekitem) on [Cache](../reference/Cache.md#diskcache_oxide.Cache) (see [Work queues](../recipes/work-queues.md)).

Tags and the eviction policy changed the on-disk format. 0.2.3 refuses a cache made by 0.1.2; since 0.3.5 such a cache opens in a versioned subdirectory instead, and [remove_old_formats()](../reference/Cache.remove_old_formats.md#diskcache_oxide.Cache.remove_old_formats) cleans up the old one (see [Upgrading across a format change](../recipes/upgrading.md)).


## Done: Windows (0.3.5)

0.3.5 adds Windows on x86_64, with wheels for all four Pythons. Everything but the fork guarantees works the same there; [Getting started](getting-started.md#windows) lists what differs. Nothing changed on disk.


## Done: upgrades across a format change (0.3.5)

A release that meets a cache in another on-disk format opens a cache of its own in a versioned subdirectory and logs a warning, instead of refusing to open, so old and new workers keep running side by side during a deploy. [remove_old_formats()](../reference/Cache.remove_old_formats.md#diskcache_oxide.Cache.remove_old_formats) cleans up the old cache once its workers are gone. See [Upgrading across a format change](../recipes/upgrading.md).


## Done: expiry that's exact for counts and listings (0.3.5)

An entry leaves `len()`, [iterkeys()](../reference/Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys), `iter()` and `reversed()` the moment it expires, as it always left [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) and `in` (D-060). Its space is still freed by the next writes or a scheduled [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire); background maintenance, below, makes that automatic.


## Next

In roughly this order:

| What | What it delivers |
|----|----|
| **Background maintenance** | A cache that looks after itself, with no scheduled [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire). One small Rust thread per process frees expired entries' space, trims a cache left over its size limit, keeps the write-ahead log small and clears stray temporary files, for every open cache. The caches are spread evenly over the interval (four caches on the default 5 minutes take turns at 0:00, 1:15, 2:30 and 3:45), the processes sharing a cache take turns so eight workers don't all do the same work, and a cache with nothing to do is only read, never locked. |
| **Django cache backend** | A diskcache-oxide backend for Django's cache framework, tested against Django's own cache test suite. It's a big job, so it gets a phase of its own. |
| **Free-threaded performance** | Make throughput scale with threads on 3.14t and 3.15t. Today a cached read spends about 60% of its time in one system call SQLite makes for its shared-memory lock, and that's the first target. |
| **`AsyncCache`** | An async API for asyncio frameworks that can't slow down the sync one, including `transact_async()`. It starts as an experiment and ships only if it earns its place; if it doesn't, this page will say why. |
| **Network server** | Stage 3, above. Speculative, and a separate product if it happens. |


## Decided against, and why

- **Least-recently-used and least-frequently-used eviction.** diskcache implements them by writing to the database on every [get](../reference/Cache.get.md#diskcache_oxide.Cache.get). Reads that never write are what let many processes read at once without queueing, so these stay out unless a design keeps reads write-free. Least-recently-stored stays the default, and `eviction_policy='none'` turns eviction off.
- **`Deque` and `Index`, until someone asks.** If you need one, open an issue. An `Index` is only as general as the key types, and both default to a temporary directory, which diskcache-oxide doesn't do.
- **`disk=` and `Disk` subclasses.** A Python class on every read and write would undo the Rust engine; `serializer=` and [register_type()](../reference/register_type.md#diskcache_oxide.register_type) cover what people used them for.
- **Pickled keys.** Keys are never pickled, so listing keys never runs pickle and a key's identity doesn't depend on pickle's bytes.
- **`read=True` and `read()`** (streaming a file in and out). Folded into the large-file cache idea below.
- **Persisted statistics.** [stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats) counts per process, in memory, because diskcache's persisted counters make every [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) a write.


# Further out

Ideas being considered, not yet committed to:

- **A large-file cache**, handing out an open file rather than a path, which is where diskcache's `read=True` would fit.
- **Encryption at rest**, if a real need appears: SQLCipher for the database, authenticated encryption for spill files.


# Non-goals

- Reading existing diskcache directories. diskcache-oxide uses its own on-disk format, so caches start cold when you switch.
- A module named `diskcache` (it would collide with the real package).
- diskcache's own `DjangoCache` class. A Django backend of diskcache-oxide's own is planned, above.
- Python older than 3.14.
