# Switching from python-diskcache

diskcache-oxide is designed to be **API compatible** with [python-diskcache](https://github.com/grantjenks/python-diskcache) 5.6.3 for [Cache](../reference/Cache.md#diskcache_oxide.Cache), but intentionally **not data compatible**. This page details the migration steps, structural differences, and deliberate improvements.


# Coexistence and on-disk files

python-diskcache stores its metadata in `cache.db`. diskcache-oxide uses its own optimized schema in `diskcache-oxide.sqlite`.

> \[!IMPORTANT\] Both engines can point to the same directory without interfering with each other, but **diskcache-oxide does not read or convert existing `cache.db` databases**. When deploying diskcache-oxide, start with a fresh directory or let diskcache-oxide create its own `diskcache-oxide.sqlite` file.

``` python
# Old code:
import diskcache
cache = diskcache.Cache('/var/cache/app')

# New code:
import diskcache_oxide
cache = diskcache_oxide.Cache('/var/cache/app')
```


# Key differences at a glance

| Behavior | python-diskcache 5.6.3 | diskcache-oxide |
|----|----|----|
| **Database file** | `cache.db` | `diskcache-oxide.sqlite` |
| **Default serialization** | `pickle` | `msgpack` (or `json`) |
| **Unpicklable / unrepresentable types** | Serialized via pickle | Raises `TypeError` (opt-in `pickle=True`) |
| **Reading pickled data without pickle** | Executes arbitrary pickle code | Raises [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) |
| **Key types** | Any picklable object (tuples, ints, etc.) | `str` only (`TypeError` otherwise) |
| **Cache truthiness** | `bool(cache)` is `len(cache) > 0` | `bool(cache)` is always `True`; `cache.enabled` is `True` |
| **[Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) exception** | Inherits only from `Exception` | Inherits from standard `TimeoutError` |
| **Minimum [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit)** | None (culls everything under ~32 KB) | 1 MiB minimum (`MIN_SIZE_LIMIT = 1048576`) |
| **[expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) behavior** | Stops early if entries share identical expiry | Removes all expired entries across chunks |
| **[clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) behavior** | Can delete concurrently written keys | Removes only entries present when clear began |
| **Fork safety** | Can crash or corrupt SQLite in child processes | Guarded by `os.register_at_fork` barrier |
| **Connection limit** | Unbounded (1 connection per thread) | Bounded by pool (`max_connections`, default 8) |
| **WAL file growth** | Unbounded under multi-process readers | Bounded by active checkpoints (`wal_size_limit`) |


# String keys only

In python-diskcache, keys could be integers, tuples, or custom objects. In diskcache-oxide:

- **All keys must be `str`**.
- Attempting to pass an `int`, `bytes`, or other key type raises a `TypeError`:

``` python
cache.set('user:100', 'data')  # OK
cache.set(100, 'data')         # Raises TypeError: cache keys must be str, not int
```

> \[!NOTE\] Keys are stored internally in a type-tagged column in SQLite, so structured key support can be introduced in future phases without requiring database migrations.


# Safe serialization by default

python-diskcache pickles every value. This causes two major issues in production:

1.  **Security**: Any process with write access to the cache directory can execute arbitrary code inside any reader process upon unpickling.
2.  **Hidden bugs**: Objects not intended to be cached (database handles, active sockets, unpicklable closures) either fail obscurely or bloat the cache.

diskcache-oxide uses `msgpack` by default (or standard `json`), with native tag support for Python types that JSON normally corrupts (tuples, sets, datetimes, 64-bit ints, and nested bytes).

``` python
# Values serialize cleanly without pickle:
cache.set('item', {
    'created': datetime.datetime.now(datetime.timezone.utc),
    'tags': {'production', 'web'},
    'coordinates': (37.7749, -122.4194)
})

# Types round-trip with exact fidelity:
val = cache.get('item')
assert isinstance(val['tags'], set)
assert isinstance(val['coordinates'], tuple)
```

If you store a custom class instance that cannot be represented in msgpack or JSON:

``` python
class CustomJob: ...

cache.set('job', CustomJob())  # Raises TypeError!
```

To enable the pickle fallback for a specific cache instance:

``` python
cache = Cache('/tmp/my-cache', pickle=True)
cache.set('job', CustomJob())  # Stored using pickle
```

When a cache configured with `pickle=False` attempts to read a row stored with pickle, it raises [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) (a `TypeError` subclass) instead of unpickling the payload.


# Cache truthiness

In python-diskcache, `bool(cache)` evaluated `len(cache) > 0`. As a result, an empty, fully-functioning cache was considered falsy:

``` python
# In python-diskcache:
cache = diskcache.Cache('/tmp/cache')
if not cache:
    print("Warning: cache is empty, but code thought caching was disabled!")
```

In diskcache-oxide:

- `bool(Cache)` is **always `True`**, and `cache.enabled` is `True`.
- `bool(NullCache)` is **always `False`**, and `null_cache.enabled` is `False`.

This allows pattern checks like `if cache:` to reliably mean "is caching enabled?".


# Timeouts and errors

In python-diskcache, [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) inherited only from base `Exception`. In diskcache-oxide:

- [diskcache_oxide.Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) subclasses Python's built-in `TimeoutError`.
- Any existing code catching [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) or `Exception` continues to work, while code catching standard `TimeoutError` now catches diskcache-oxide timeouts as well.


# Testing with custom clocks

python-diskcache read `time.time()` directly from Python, which allowed test suites to manipulate time using `unittest.mock.patch('time.time')`. However, calling Python's clock on every hot read incurred noticeable overhead.

diskcache-oxide reads the system monotonic/wall clock natively in Rust for maximum speed. To test expiration logic with monkeypatched clocks, pass the `clock=` argument:

``` python
import time
from unittest.mock import patch
from diskcache_oxide import Cache

with patch('time.time') as mock_time:
    mock_time.return_value = 1000.0
    cache = Cache('/tmp/test-cache', clock=lambda: time.time())
    cache.set('k', 'v', expire=10)

    mock_time.return_value = 1015.0
    assert cache.get('k') is None  # Expired according to the mock clock
```
