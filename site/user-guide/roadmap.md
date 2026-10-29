# Project status and roadmap

diskcache-oxide is an active, clean-room reimplementation of python-diskcache in Rust. It is currently approximately **60% complete**.

This page outlines what is fully implemented and tested today, and what features are scheduled for future phases.


# What is complete today (Phase 0 - Phase 2)

The core foundation and the primary [Cache](../reference/Cache.md#diskcache_oxide.Cache) interface are fully operational, tested against both diskcache 5.6.3 and the diskcache-community fork across multiple Python versions (3.14, 3.14t, 3.15, 3.15t):

- **Core [Cache](../reference/Cache.md#diskcache_oxide.Cache) operations**:
  - [get()](../reference/NullCache.get.md#diskcache_oxide.NullCache.get), [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set), [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add), [delete()](../reference/Cache.delete.md#diskcache_oxide.Cache.delete), [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop), [touch()](../reference/Cache.touch.md#diskcache_oxide.Cache.touch).
  - Atomic counters: [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr), [decr()](../reference/Cache.decr.md#diskcache_oxide.Cache.decr).
  - Fast prefix iteration: `iterkeys(prefix=...)`.
  - Dictionary interface: `cache[k]`, `k in cache`, `del cache[k]`, `len(cache)`, `bool(cache)`.
  - Resource cleanup: [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close), context manager (`with Cache(...) as cache:`).
- **Storage and file spills**:
  - Inline small values in SQLite; automatic file spilling for large values.
  - Atomic temporary file creation and rename.
  - Race-condition safety: readers racing an overwrite always receive consistent old or new data.
- **Serialization & Data Safety**:
  - `serializer="msgpack"` (default): ultra-fast binary format.
  - `serializer="json"`: standard JSON with float roundtrip.
  - Type-tagging system: exact preservation of `tuple`, [set](../reference/Cache.set.md#diskcache_oxide.Cache.set), `datetime`, `NaN`/`inf`, nested `bytes`, and large ints.
  - Safe by default: `pickle=False` default; `pickle=True` opt-in.
  - [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) safety check for non-pickle handles.
- **Process and thread concurrency**:
  - `#[pymodule(gil_used = false)]` for free-threaded Python 3.14t and 3.15t.
  - Process-wide fork barrier via `os.register_at_fork`: safe for Gunicorn, Celery, and Uvicorn.
  - Bounded connection pool (`max_connections`, default 8).
- **Resource bounds**:
  - Bounded SQLite page cache per connection (`sqlite_cache_size`).
  - Active forced WAL checkpoints (`wal_size_limit`, default 64 MiB).
  - Least-Recently-Stored (LRS) eviction with [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) and [cull_limit](../reference/Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit).
- **[NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) & Typing**:
  - Complete [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) implementation with instant-expiry semantics and `invalidate=` forwarding.
  - [CacheLike](../reference/CacheLike.md#diskcache_oxide.CacheLike) protocol for static type checkers.
  - Shipped with `py.typed`.

------------------------------------------------------------------------


# In progress


## Phase 2.5: Concurrency deep review and soak testing

- Long-duration multi-hour soak tests (`soak/`) running multi-process writers, readers, and random SIGKILL crash recovery.
- Concurrency audit of SQLite WAL lock transitions and edge-case deadlocks.
- Proactive background sweeper thread: a lightweight Rust background thread per cache store to proactively purge expired entries and run periodic WAL checkpoints off the hot request path.


## Platform expansion: Windows support

- Porting the fork barrier and file inode metadata handling to Windows.

------------------------------------------------------------------------


# Upcoming roadmap


## Phase 3: Production adoption

- Initial deployment and validation across high-traffic production web applications at Talk Python.
- Real-world validation of WAL bounds, latency, and cache hit rates under live web traffic.


## Phase 4: Free-threaded optimization

- Deep profiling on multi-core workloads with CPython 3.14t / 3.15t.
- Evaluating zero-GIL lockless read paths and memory cache optimization.


## Phase 5: `AsyncCache`

- Native async interface (`AsyncCache`) designed for asynchronous web frameworks: FastAPI, Starlette, Litestar, Quart, and Sanic.
- Non-blocking disk I/O offloaded to background thread pools without slowing down the synchronous path.


## Phase 6: Parity features

- **Phase 6a: `FanoutCache` & Structured Keys**: Sharding across multiple SQLite databases for extreme write concurrency, plus arbitrary key types beyond `str`.
- **Phase 6b: Tags**: Tagging cached entries and invalidating groups of keys by tag (`cache.evict(tag=...)`).
- **Phase 6c: Data structures**: `Deque` and `Index` disk-backed collections.
- **Phase 6d: Recipes**: `@cache.memoize()` function decorators, rate limiters, distributed locks.
- **`transact()`**: Full multi-statement write transactions.


## Phase 7: Standalone network cache server

- A high-performance, standalone network cache server powered by diskcache-oxide's Rust core, providing a disk-backed alternative to Redis with minimal operational overhead.
