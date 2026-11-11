## FanoutCache.evict()


Remove every entry tagged `tag` that existed when the call started, from every shard, as


Usage

``` python
FanoutCache.evict(
    tag,
    *,
    timeout=None,
    retry=False,
)
```


[Cache.evict](Cache.evict.md#diskcache_oxide.Cache.evict).


## Parameters


`tag: Key`  
The tag.

`timeout: float | None = None`  
As for [expire](Cache.expire.md#diskcache_oxide.Cache.expire): one budget for the whole call.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Returns


`int`  
How many entries were removed.


## Raises


`TypeError`  
`tag` isn't a key type.

`Timeout`  
As for [expire](Cache.expire.md#diskcache_oxide.Cache.expire), with `removed` and `failed_shards`.
