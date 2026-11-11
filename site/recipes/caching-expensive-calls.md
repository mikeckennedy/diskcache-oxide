# Caching expensive calls

The page takes 900 ms, and 850 of them are one query that returns the same ten rows it returned a second ago. Everyone has met this page. Caching is how you stop paying for the same answer twice: before doing something slow (a database query, an API call, rendering a big page), ask the cache. If it knows, you're done in a microsecond or two. If not, do the work and leave the answer for whoever asks next, which with a shared directory means every worker process on the machine, today and after the next restart.


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


    first call:  203.5 ms
    cached:      1.1 µs per call


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


# A caching decorator: [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize)

The same pattern wrapped up for reuse. [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) keys each call on the function's full name and its arguments, and caches `None` results too.


``` python
calls = 0

@cache.memoize(expire=600)
def episode_details(episode_id: int, *, include_transcript: bool = False) -> dict:
    global calls
    calls += 1
    return {'id': episode_id, 'transcript': include_transcript}

episode_details(512)
episode_details(512)
episode_details(512, include_transcript=True)
print(f'{calls} real calls for 3 lookups')

# The key a call uses, to forget one result:
print(episode_details.__cache_key__(512))
cache.delete(episode_details.__cache_key__(512))
```


    2 real calls for 3 lookups
    ('__main__.episode_details', 512, None)


    True


Call it with parentheses, `@cache.memoize()`, even with no options. It's diskcache's [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize), with the same keys, so code written for diskcache keeps working.

A few things to keep in mind:

- **Arguments become part of the key**, so they must be key types: `str`, `bytes`, `int`, `float`, `bool`, `None`, or tuples of those. Anything else (a list, a date, an object) raises `TypeError` before the function runs. Pass a tuple instead of a list, and leave out arguments that don't change the answer, like a database connection, with `ignore=` (`@cache.memoize(ignore=('db',))`).
- **Keyword and positional calls are different entries:** `f(1, 2)` and `f(1, b=2)` are cached separately, as in diskcache.
- **Return values must be storable**, which means built-in types (dicts, lists, tuples, sets, datetimes and so on). A dataclass or ORM object raises `TypeError` at [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set): return a dict instead, or use a cache opened with `pickle=True`. See [Serialization](../user-guide/serialization.md).
- **Long keys are fine.** A key is just indexed bytes.


# Thundering herds

When a popular entry expires, every worker that asks for it in the next moment misses at the same time, and all of them recompute it. For most values that's harmless. If the computation is expensive enough that doing it eight times at once hurts, refresh it ahead of expiry from a background job (see [Running a job once across workers](run-once.md)), so readers never see it missing.

`memoize_stampede(cache, expire=...)` is [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) with that built in: a result close to expiring is recomputed early, in the background, by one caller, while every caller keeps getting the cached one. See [Coordination](../user-guide/coordination.md#memoize_stampede).
