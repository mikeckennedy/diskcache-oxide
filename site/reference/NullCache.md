## NullCache


A switched-off cache, so app code needs no `if cache is None` checks.


Usage

``` python
NullCache(
    directory=None,
    timeout=None,
    *,
    size_limit=None,
    cull_limit=None,
    serializer="msgpack",
    pickle=False,
    clock=None,
    max_connections=8,
    sqlite_cache_size=None,
    wal_size_limit=None,
    wal_checkpoint="manual",
    on_decode_error=None,
    durable=False,
    invalidate=None
)
```


It behaves like a real cache in which every entry expires the instant it's written and is swept straight away: reads miss, writes succeed, and it's always empty. It's falsy, and its [enabled](Cache.enabled.md#diskcache_oxide.Cache.enabled) is False.


## Parameters


`directory: str | os.PathLike[str] | None = None`  
Accepted and ignored, like every other [Cache](Cache.md#diskcache_oxide.Cache) argument, so switching between the two is a one-line change.

`invalidate: CacheLike | None = None`  
A real cache to forward removals to ([delete](Cache.delete.md#diskcache_oxide.Cache.delete), [pop](Cache.pop.md#diskcache_oxide.Cache.pop), [clear](Cache.clear.md#diskcache_oxide.Cache.clear) and [iterkeys](Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys)), so invalidations still land while caching is off and nothing stale is served once it's back on.
