## NullCache.add()


Return True without storing anything: the key is always absent, so the add succeeds.


Usage

``` python
NullCache.add(
    key,
    value,
    expire=None,
    *,
    retry=False,
)
```


With caching off, every caller's [add()](Cache.add.md#diskcache_oxide.Cache.add) succeeds, so don't rely on it for job claims or locks. A `Cache(durable=True)` does the opposite when a write fails: its [add()](Cache.add.md#diskcache_oxide.Cache.add) returns False.
