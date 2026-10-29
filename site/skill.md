---
name: diskcache-oxide
description: >
  A clean-room Rust reimplementation of diskcache. Use when writing Python code that uses the diskcache_oxide package.
license: MIT
compatibility: Requires Python >=3.14.
---

# diskcache-oxide

A clean-room Rust reimplementation of diskcache.

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
- `Cache.close`
- `Cache.__getitem__`
- `Cache.__setitem__`
- `Cache.__delitem__`
- `Cache.__contains__`
- `Cache.__len__`
- `Cache.__bool__`
- `Cache.__iter__`
- `Cache.__enter__`
- `Cache.__exit__`

### NullCache

Switched-off cache behaving as if every entry expires instantly.

- `NullCache`: A switched-off cache. See the module docstring for the rule every method follows
- `CacheLike`: What application code can rely on from a `Cache` or a `NullCache`

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

### Errors

Exceptions raised by cache operations.

- `Timeout`: The database stayed locked for longer than the cache's timeout
- `UnsafeValueError`: A pickle=False cache read a value that was stored with pickle
- `CorruptValueError`: A stored value couldn't be decoded: an unknown mode, or bytes that don't parse
- `FormatMismatchError`: The cache directory holds a database in a format this version doesn't support

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
- Leave wal_size_limit and sqlite_cache_size at their defaults unless tuning for extreme read-heavy or memory-constrained workloads.

## What diskcache-oxide is

A clean-room reimplementation of [python-diskcache](https://github.com/grantjenks/python-diskcache)
in Rust via PyO3, with a minimal Python layer (`import diskcache_oxide`, PyPI `diskcache-oxide`).
It is API compatible with `diskcache.Cache`, but intentionally not data compatible: it uses its own
on-disk format and separate database file (`diskcache-oxide.sqlite`), roughly halving read latency,
providing higher multithreaded throughput, safe cross-process concurrency, fork safety, and
free-threaded CPython (3.14t/3.15t) support with `gil_used = false`.

> **Status:** ~60% complete (pre-release). The core `Cache`, `NullCache`, file spilling, msgpack/JSON
> serialization, fork barrier, connection pool, and WAL limits are fully working and conformance tested.
> `FanoutCache`, `transact()`, tags, non-str keys, `Deque`/`Index`, and recipes come in subsequent phases.

## Quickstart

```python
from diskcache_oxide import Cache

# Always use a context manager or close() when done
with Cache('/tmp/my-app-cache') as cache:
    # Storing values (msgpack by default; JSON also supported)
    cache.set('user:42', {'name': 'Alice', 'role': 'admin'}, expire=3600)

    # Reading values
    user = cache.get('user:42')
    print(user['name'])  # Alice

    # Dictionary syntax works identically
    cache['session:xyz'] = {'active': True}
    if 'session:xyz' in cache:
        print(cache['session:xyz'])
```

## Switching from python-diskcache

1. **Point to a fresh directory**: diskcache-oxide stores data in `diskcache-oxide.sqlite` (diskcache
   uses `cache.db`). Both can coexist in one parent directory without collisions, but diskcache-oxide
   does not read old diskcache databases.
2. **String keys only**: keys must be `str`. Integers, bytes, or tuples as keys raise `TypeError`.
3. **No pickle by default**: values are stored using `msgpack` (default) or `json` with type tags
   for sets, tuples, datetimes, and big ints. If unpicklable types are passed, `set()` raises
   `TypeError`. Pass `pickle=True` if you explicitly need pickle fallback.
4. **Truthy cache**: `bool(Cache)` is always `True` (diskcache is falsy when empty).
5. **TimeoutError**: `diskcache_oxide.Timeout` subclasses Python's standard `TimeoutError`.

```python
# Old:
# import diskcache
# cache = diskcache.Cache('/tmp/cache')

# New:
import diskcache_oxide
cache = diskcache_oxide.Cache('/tmp/cache', serializer='msgpack')
```

## The NullCache pattern

Use `NullCache` to turn caching off entirely (e.g. in tests or local development) without altering
call sites or adding `if cache is not None:` guards everywhere:

```python
import os
from diskcache_oxide import Cache, NullCache, CacheLike

def get_cache(path: str) -> CacheLike:
    if os.getenv('DISABLE_CACHE'):
        return NullCache(path)
    return Cache(path)

cache = get_cache('/tmp/app')
# Works identically, but every entry expires instantly
cache.set('k', 'v')
assert cache.get('k') is None
assert bool(cache) is False  # if cache: checks if caching is enabled
```

### Invalidation forwarding with `invalidate=`

When caching is toggled off, passing `invalidate=real_cache` forwards `delete()`, `pop()`,
`clear()`, and `iterkeys()` to the real cache so mutations don't leave stale entries behind:

```python
real_cache = Cache('/tmp/cache')
null = NullCache(invalidate=real_cache)

null.delete('user:42')  # deletes from real_cache!
assert null.get('user:42') is None  # reads remain disabled
```

## Key gotchas

- **Keys must be `str`**: non-string keys raise `TypeError`.
- **Pickle is disabled by default**: storing objects that msgpack/JSON cannot serialize raises
  `TypeError`. Pass `pickle=True` on `Cache(...)` only if necessary.
- **Reading pickled data on `pickle=False`**: raises `UnsafeValueError` (a `TypeError` subclass)
  rather than executing unpickling code.
- **`size_limit` minimum**: minimum is 1 MiB (`1048576` bytes). Values below this raise `ValueError`.
- **Atomic counters**: `incr()` and `decr()` operate atomically across processes. If a key is absent
  and `default=None`, `KeyError` is raised.
- **Fork safety**: fork is guarded by an `os.register_at_fork` barrier that blocks forks during
  write transactions and closes/reopens SQLite connection handles safely in child processes.
- **Iterkeys prefix scan**: `iterkeys(prefix="user:")` efficiently scans matching keys without
  loading the whole database into memory.


## Resources

- [Full documentation](https://mkennedy.codes/docs/diskcache-oxide/)
- [llms.txt](llms.txt) — Indexed API reference for LLMs
- [llms-full.txt](llms-full.txt) — Comprehensive documentation for LLMs
- [Source code](https://github.com/mikeckennedy/diskcache-oxide)
