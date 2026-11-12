## FanoutCache.clear()


Remove every entry that existed when the call started, from every shard.


Usage

``` python
FanoutCache.clear(
    *,
    timeout=None,
    retry=False,
)
```


Every shard's starting point is taken before anything is removed, so writes that land while it runs, on any shard, are kept, as on [Cache.clear](Cache.clear.md#diskcache_oxide.Cache.clear).


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
