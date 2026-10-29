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

null.delete('user:42')               # deleted from real_cache
assert null.get('user:42') is None   # reads stay disabled
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
