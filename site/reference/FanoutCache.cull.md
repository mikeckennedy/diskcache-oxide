## FanoutCache.cull()


Remove expired entries, then evict the least recently stored from each shard until it's


Usage

``` python
FanoutCache.cull(
    *,
    timeout=None,
    retry=False,
)
```


under its share of [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit).


## Parameters


`timeout: float | None = None`  
As for [expire](Cache.expire.md#diskcache_oxide.Cache.expire): one budget for the whole call.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Returns


`int`  
How many entries were removed.


## Raises


`Timeout`  
As for [expire](Cache.expire.md#diskcache_oxide.Cache.expire), with `removed` and `failed_shards`.
