---
name: diskcache-oxide
description: >
  A shared, durable disk cache for Python, with a Rust engine, SQLite storage, and a diskcache-compatible API. Use when writing Python code that uses the diskcache_oxide package.
license: MIT
compatibility: Requires Python >=3.14.
---

# diskcache-oxide

A shared, durable disk cache for Python, with a Rust engine, SQLite storage, and a diskcache-compatible API.

## Installation

```bash
pip install diskcache-oxide
```

## API overview

### Cache

Disk-backed cache backed by SQLite, shared safely across threads and processes.

- `Cache`: A disk cache backed by SQLite, shared safely across threads and processes

### Cache Methods

Methods for the Cache class

- `Cache.directory`
- `Cache.timeout`
- `Cache.size_limit`
- `Cache.cull_limit`
- `Cache.enabled`
- `Cache.set`
- `Cache.add`
- `Cache.delete`
- `Cache.pop`
- `Cache.touch`
- `Cache.incr`
- `Cache.decr`
- `Cache.iterkeys`
- `Cache.expire`
- `Cache.clear`
- `Cache.cull`
- `Cache.volume`
- `Cache.checkpoint`
- `Cache.close`
- `Cache.transact`
- `Cache.__getitem__`
- `Cache.__setitem__`
- `Cache.__delitem__`
- `Cache.__contains__`
- `Cache.__len__`
- `Cache.__bool__`
- `Cache.__iter__`
- `Cache.__enter__`
- `Cache.__exit__`
- `Cache.get`

### FanoutCache

A cache sharded over several SQLite databases, so writers in many processes stop queueing on one write lock.

- `FanoutCache`: A disk cache split over several SQLite databases (shards), so reads and writes on different

### FanoutCache Methods

Methods for the FanoutCache class

- `FanoutCache.directory`
- `FanoutCache.timeout`
- `FanoutCache.shards`
- `FanoutCache.size_limit`
- `FanoutCache.cull_limit`
- `FanoutCache.enabled`
- `FanoutCache.shard_of`
- `FanoutCache.transact`
- `FanoutCache.set`
- `FanoutCache.add`
- `FanoutCache.delete`
- `FanoutCache.pop`
- `FanoutCache.touch`
- `FanoutCache.incr`
- `FanoutCache.decr`
- `FanoutCache.volume`
- `FanoutCache.expire`
- `FanoutCache.clear`
- `FanoutCache.cull`
- `FanoutCache.checkpoint`
- `FanoutCache.iterkeys`
- `FanoutCache.__iter__`
- `FanoutCache.close`
- `FanoutCache.__getitem__`
- `FanoutCache.__setitem__`
- `FanoutCache.__delitem__`
- `FanoutCache.__contains__`
- `FanoutCache.__len__`
- `FanoutCache.__bool__`
- `FanoutCache.__enter__`
- `FanoutCache.__exit__`
- `FanoutCache.get`

### NullCache

Switched-off cache behaving as if every entry expires instantly.

- `NullCache`: A switched-off cache, so app code needs no `if cache is None` checks
- `CacheLike`: What application code can rely on from a `Cache`, a `FanoutCache` or a `NullCache`
- `TransactionalCacheLike`: A `CacheLike` with `transact()`, which `Cache` and `NullCache` both have
- `ShardedCacheLike`: A `CacheLike` with a sharded cache's `transact(*, key, retry=False)`, which `FanoutCache` and

### NullCache Methods

Methods for the NullCache class

- `NullCache.__repr__`
- `NullCache.directory`
- `NullCache.timeout`
- `NullCache.size_limit`
- `NullCache.cull_limit`
- `NullCache.enabled`
- `NullCache.get`
- `NullCache.__getitem__`
- `NullCache.__contains__`
- `NullCache.__len__`
- `NullCache.__bool__`
- `NullCache.__iter__`
- `NullCache.volume`
- `NullCache.checkpoint`
- `NullCache.set`
- `NullCache.__setitem__`
- `NullCache.add`
- `NullCache.touch`
- `NullCache.incr`
- `NullCache.decr`
- `NullCache.delete`
- `NullCache.__delitem__`
- `NullCache.pop`
- `NullCache.clear`
- `NullCache.iterkeys`
- `NullCache.transact`
- `NullCache.expire`
- `NullCache.cull`
- `NullCache.close`
- `NullCache.__enter__`
- `NullCache.__exit__`

### CacheLike Methods

Methods for the CacheLike class

- `CacheLike.directory`
- `CacheLike.timeout`
- `CacheLike.size_limit`
- `CacheLike.cull_limit`
- `CacheLike.enabled`
- `CacheLike.get`
- `CacheLike.set`
- `CacheLike.add`
- `CacheLike.delete`
- `CacheLike.pop`
- `CacheLike.touch`
- `CacheLike.incr`
- `CacheLike.decr`
- `CacheLike.iterkeys`
- `CacheLike.expire`
- `CacheLike.clear`
- `CacheLike.cull`
- `CacheLike.volume`
- `CacheLike.checkpoint`
- `CacheLike.close`
- `CacheLike.__getitem__`
- `CacheLike.__setitem__`
- `CacheLike.__delitem__`
- `CacheLike.__contains__`
- `CacheLike.__len__`
- `CacheLike.__bool__`
- `CacheLike.__iter__`
- `CacheLike.__enter__`
- `CacheLike.__exit__`

### Caches

A registry of named caches under one base directory.

- `Caches`: A registry of named caches under one base directory
- `CacheStats`: One cache's numbers, for an admin page. `error` says why it couldn't be read, if it couldn't

### Caches Methods

Methods for the Caches class

- `Caches.__repr__`
- `Caches.register`
- `Caches.__contains__`
- `Caches.__iter__`
- `Caches.__len__`
- `Caches.directory`
- `Caches.__getitem__`
- `Caches.fanout`
- `Caches.disable`
- `Caches.enable`
- `Caches.enabled`
- `Caches.stats`
- `Caches.clear`
- `Caches.expire_all`
- `Caches.close_all`
- `Caches.__enter__`
- `Caches.__exit__`

### Errors

Exceptions raised by cache operations.

- `Timeout`: A lock stayed held, or a fork stayed pending, for longer than the cache's timeout
- `TransactionBusy`: A thread holding one cache's transaction couldn't begin another's because another thread holds its write lock
- `UnsafeValueError`: A pickle=False cache read a value that was stored with pickle
- `CorruptValueError`: A stored value couldn't be decoded: an unknown mode, or bytes that don't parse
- `ForeignValueError`: A stored value this build can't read but another might: an unknown mode, tag or extension (a newer version wrote it), or a time zone missing from this host
- `FormatMismatchError`: The cache directory holds a database in a format this version doesn't support
- `LayoutMismatchError`: The directory's layout doesn't match how it was opened: a plain Cache on a FanoutCache root or shard, a FanoutCache on a plain Cache's directory, or shards= that differs from the stored count. Also a ValueError

### SQLite and System

Information about the bundled SQLite and environment.

- `sqlite_version`: The SQLite version reported at runtime by `sqlite3_libversion()`
- `sqlite_compile_options`: The compile-time options of the linked SQLite (`PRAGMA compile_options`)
- `BUNDLED_SQLITE_VERSION`: str(object='') -> str

## Gotchas

1. diskcache-oxide accepts only str keys for now; non-string keys raise TypeError.
2. pickle is disabled by default: storing unrepresentable types raises TypeError at set time unless Cache(..., pickle=True) is passed.
3. Reading pickled data with a pickle=False cache raises UnsafeValueError (a TypeError subclass) rather than executing arbitrary unpickle code.
4. bool(Cache) is always True and Cache.enabled is True (unlike python-diskcache where an empty cache is falsy). bool(NullCache) is False.
5. diskcache-oxide uses diskcache-oxide.sqlite (not cache.db); it does not read existing diskcache files.
6. size_limit must be at least 1 MiB (MIN_SIZE_LIMIT); passing smaller raises ValueError.
7. Timeout inherits from TimeoutError (unlike python-diskcache which only inherits from Exception).
8. NullCache behaves as if every entry expires the moment it is written and is instantly swept: get() returns default, set() returns True, volume() is 0.
9. NullCache(invalidate=real_cache) forwards deletes and clears to the underlying cache while keeping caching off.

## Best practices

- Always use a context manager (with Cache(...) as cache:) or call cache.close() to cleanly return SQLite connections.
- Use serializer='msgpack' (the default) for maximum performance and compactness, or serializer='json' if human inspection in SQLite is desired.
- Use NullCache in tests or local dev environments to disable caching cleanly without adding conditional checks in application code.
- Type application functions that accept either cache type with CacheLike from diskcache_oxide.protocol.
- Call cache.checkpoint() at shutdown or from a maintenance job to empty the write-ahead log after write bursts; it returns False rather than raising when other processes keep it busy.
- Leave wal_size_limit and sqlite_cache_size at their defaults unless tuning for extreme read-heavy or memory-constrained workloads.
- Use Cache(..., on_decode_error='miss') (or a reporting function) so a value that can't be decoded, such as a pickled class that moved, is a miss instead of an exception.
- For several worker processes that write often, use FanoutCache(directory, shards=8) (or Caches.register(name, shards=8), read with caches.fanout(name)); shards= is keyword-only and permanent for a directory, and transact(key=...) covers only that key's shard.
- Register an app's caches once in a Caches registry and look them up by name (caches['general']); a cache that can't open becomes a NullCache and is retried after 10 seconds.

## What diskcache-oxide is

A shared, durable disk cache for Python, built on a Rust engine (PyO3) and SQLite storage, with a minimal Python layer (`import diskcache_oxide`, distribution `diskcache-oxide`). It is API compatible with [python-diskcache](https://github.com/grantjenks/python-diskcache)'s `Cache`, but deliberately not data compatible: it uses its own database file (`diskcache-oxide.sqlite`) and never reads diskcache's `cache.db`. Every thread and process that opens the same directory shares one cache. Compared with diskcache it has about half the read latency, much higher threaded throughput, fork safety, bounded memory and WAL size, and free-threaded CPython (3.14t/3.15t) support with `gil_used = false`.

> **Status:** pre-release, not yet on PyPI (install from GitHub; building needs a Rust toolchain). The core `Cache`, `NullCache`, large-value files, msgpack/JSON serialization, the fork barrier, the connection pool and WAL management are working and conformance tested against diskcache 5.6.3. `FanoutCache`, `AsyncCache`, `transact()`, tags, `memoize`, non-str keys, `Deque`/`Index` and the recipes (locks, throttles) are planned, not available.

## Quickstart

```python
from diskcache_oxide import Cache

with Cache('/var/cache/my-app') as cache:
    cache.set('user:42', {'name': 'Sam', 'role': 'admin'}, expire=3600)
    user = cache.get('user:42')

    cache['session:xyz'] = {'active': True}
    if 'session:xyz' in cache:
        print(cache['session:xyz'])

    # Atomic across processes: exactly one caller wins.
    if cache.add('job:nightly', 'claimed', expire=3600):
        run_nightly_job()

    # Optional, at shutdown: empty the write-ahead log. Returns False if other processes keep it busy.
    cache.checkpoint()
```

## Switching from python-diskcache

1. **Use a fresh directory, or accept a cold start.** diskcache-oxide stores data in `diskcache-oxide.sqlite` and never reads diskcache's `cache.db`.
2. **String keys only.** Non-`str` keys raise `TypeError` for now.
3. **No pickle by default.** Values are stored as msgpack (default) or JSON with type tags for tuples, sets, datetimes, big ints, NaN and non-str dict keys. A value that can't be represented raises `TypeError` at `set()`. Pass `pickle=True` to allow a pickle fallback.
4. **`bool(Cache)` is always True** (diskcache is falsy when empty).
5. **`Timeout` subclasses `TimeoutError`.**
6. **`directory` is required**, and there's no `disk=` or `eviction_policy=` argument.
7. **Tests that patch `time.time`** must pass `clock=lambda: time.time()`, because the cache reads the clock natively.

```python
# from diskcache import Cache
from diskcache_oxide import Cache

cache = Cache('/var/cache/my-app-oxide')
```

## The NullCache pattern

`NullCache` turns caching off without changing call sites. It behaves like a cache where every entry expires the instant it's written: `get` returns the default, `set` and `add` return True, `len` is 0, and `bool(cache)` is False. Its constructor accepts and ignores `Cache`'s arguments.

```python
import os
from diskcache_oxide import Cache, CacheLike, NullCache


def get_cache(path: str) -> CacheLike:
    if os.getenv('DISABLE_CACHE'):
        return NullCache(path)
    return Cache(path)
```

Because `add()` always succeeds on a `NullCache`, don't rely on `add()` for job claims or locks on a cache that might be switched off.

### Invalidation forwarding with `invalidate=`

`NullCache(invalidate=real_cache)` forwards `delete()`, `pop()` (as a delete), `clear()` and `iterkeys()` to the real cache, so invalidations still land while caching is off:

```python
real_cache = Cache('/var/cache/my-app')
null = NullCache(invalidate=real_cache)

null.delete('user:42')  # deleted from real_cache
assert null.get('user:42') is None  # reads stay disabled
```

## Key gotchas

- **Keys must be `str`.**
- **Pickle is off by default.** Only exact built-in types round-trip; a `dict` subclass or a custom class raises `TypeError` unless `pickle=True`.
- **Reading a pickled value with `pickle=False`** raises `UnsafeValueError` (a `TypeError` subclass) and never unpickles.
- **`size_limit` minimum** is 1 MiB; smaller raises `ValueError`.
- **`incr()`/`decr()`** are atomic across processes and keep the entry's expiry. With `default=None`, a missing key raises `KeyError`.
- **`expire=True` / `timeout=True`** raise `TypeError` (diskcache treats them as 1).
- **Fork safety:** an `os.register_at_fork` barrier waits for other threads' write transactions before a fork, and the child drops its inherited connections and opens fresh ones. No action needed.
- **`iterkeys(prefix=...)`** is an indexed range scan in sorted order; iterating the cache directly yields insertion order.
- **WAL size:** with readers in other processes, write bursts can leave a large `-wal` file. Call `cache.checkpoint()` at shutdown or in a maintenance job; `close()` checkpoints a large WAL automatically.


## Resources

- [Full documentation](https://mkennedy.codes/docs/diskcache-oxide/)
- [llms.txt](llms.txt) — Indexed API reference for LLMs
- [llms-full.txt](llms-full.txt) — Comprehensive documentation for LLMs
- [Source code](https://github.com/mikeckennedy/diskcache-oxide)
