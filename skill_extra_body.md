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
