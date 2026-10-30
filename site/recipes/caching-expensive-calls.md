# Caching expensive calls

The most common use of a cache: before doing something slow (a database query, an API call, rendering a big page), check whether the answer is already cached. If it is, use it. If not, compute it and store it for next time. Every worker process on the machine shares the result, and it survives restarts.


# Cache-aside


``` python
import tempfile
import time

from diskcache_oxide import Cache

cache = Cache(tempfile.mkdtemp())

def load_popular_episodes() -> list[dict]:
    time.sleep(0.2)  # stands in for a slow database query
    return [{'id': 512, 'title': 'Rust for Python developers'}]

def popular_episodes() -> list[dict]:
    episodes = cache.get('episodes:popular')
    if episodes is None:
        episodes = load_popular_episodes()
        cache.set('episodes:popular', episodes, expire=300)
    return episodes

start = time.perf_counter()
popular_episodes()
print(f'first call:  {(time.perf_counter() - start) * 1000:.1f} ms')

start = time.perf_counter()
for _ in range(1000):
    popular_episodes()
print(f'cached:      {(time.perf_counter() - start) * 1000:.1f} µs per call')  # 1000 calls, so ms total = µs each
```


    first call:  211.1 ms
    cached:      3.3 µs per call


Always give cached values an [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire). It bounds how stale a value can get if an invalidation is ever missed.


# When `None` is a real value

If the function can legitimately return `None`, `cache.get(key)` can't tell "cached as `None`" from "not cached", and you'll recompute every time. Use a sentinel default:

``` python
MISSING = object()

value = cache.get(key, default=MISSING)
if value is MISSING:
    value = compute()
    cache.set(key, value, expire=300)
```


# A caching decorator

The same pattern wrapped up for reuse. The key is the function's full name plus its arguments, encoded as JSON so that different arguments always give different keys.


``` python
import functools
import json
from collections.abc import Callable
from typing import Any

from diskcache_oxide import CacheLike

MISSING = object()

def cached(cache: CacheLike, expire: float | None = None) -> Callable:
    """Cache a function's results, keyed by its arguments."""

    def decorator(func: Callable) -> Callable:
        prefix = f'{func.__module__}.{func.__qualname__}:'

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = prefix + json.dumps([args, kwargs], sort_keys=True, default=str)
            value = cache.get(key, default=MISSING)
            if value is MISSING:
                value = func(*args, **kwargs)
                cache.set(key, value, expire=expire)
            return value

        return wrapper

    return decorator

calls = 0

@cached(cache, expire=600)
def episode_details(episode_id: int, *, include_transcript: bool = False) -> dict:
    global calls
    calls += 1
    return {'id': episode_id, 'transcript': include_transcript}

episode_details(512)
episode_details(512)
episode_details(512, include_transcript=True)
print(f'{calls} real calls for 3 lookups')
```


    2 real calls for 3 lookups


A few things to keep in mind:

- **Arguments become part of the key**, so keep them simple: ids, strings, numbers. `default=str` covers values like dates, but two different objects with the same `str()` would share a key.
- **Return values must be storable**, which means built-in types (dicts, lists, tuples, sets, datetimes and so on). A dataclass or ORM object raises `TypeError` at [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set): return a dict instead, or use a cache opened with `pickle=True`. See [Serialization](../user-guide/serialization.md).
- **Long keys are fine.** A key is just an indexed string.


# Thundering herds

When a popular entry expires, every worker that asks for it in the next moment misses at the same time, and all of them recompute it. For most values that's harmless. If the computation is expensive enough that doing it eight times at once hurts, refresh it ahead of expiry from a background job (see [Running a job once across workers](run-once.md)), so readers never see it missing.

A `memoize` with built-in stampede protection is planned for Phase 6.
