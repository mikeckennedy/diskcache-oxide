## memoize_stampede()


[memoize](Cache.memoize.md#diskcache_oxide.Cache.memoize) with cache stampede protection, as diskcache's: a result close to expiry is


Usage

``` python
memoize_stampede(
    cache, expire, name=None, typed=False, tag=None, beta=1, ignore=()
)
```


recomputed early, in a background thread, by one caller (Vattani et al., VLDB 2015), while every caller keeps getting the cached result. Results are stored as `(result, seconds it took)`.

Keys, `typed`, `ignore` and the argument rules are [memoize](Cache.memoize.md#diskcache_oxide.Cache.memoize)'s (D-048). It works on a [NullCache](NullCache.md#diskcache_oxide.NullCache) (every call runs) and a durable handle (an outage runs the function), as [memoize](Cache.memoize.md#diskcache_oxide.Cache.memoize) does. The time left is measured with `time.time()`, as diskcache's, so a cache opened with a `clock=` that isn't wall time recomputes on that clock's expiry but judges "close to expiring" by wall time.
