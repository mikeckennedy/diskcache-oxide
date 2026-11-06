## FanoutCache.cull()


Remove expired entries, then evict the least recently stored from each shard until it's


Usage

``` python
FanoutCache.cull(
    *,
    timeout=None,
)
```


under its share of [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit).


## Parameters


`timeout: float | None = None`  
As for [expire](Cache.expire.md#diskcache_oxide.Cache.expire): one budget for the whole call.


## Returns


`int`  
How many entries were removed.


## Raises


`Timeout`  
As for [expire](Cache.expire.md#diskcache_oxide.Cache.expire), with `removed` and `failed_shards`.
