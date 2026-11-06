## FanoutCache.clear()


Remove every entry that existed when the call started, from every shard.


Usage

``` python
FanoutCache.clear(
    *,
    timeout=None,
)
```


Every shard's starting point is taken before anything is removed, so writes that land while it runs, on any shard, are kept, as on [Cache.clear](Cache.clear.md#diskcache_oxide.Cache.clear).


## Parameters


`timeout: float | None = None`  
As for [expire](Cache.expire.md#diskcache_oxide.Cache.expire): one budget for the whole call.


## Returns


`int`  
How many entries were removed.


## Raises


`Timeout`  
As for [expire](Cache.expire.md#diskcache_oxide.Cache.expire), with `removed` and `failed_shards`.
