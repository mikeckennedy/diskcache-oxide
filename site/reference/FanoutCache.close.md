## FanoutCache.close()


Close this handle on every shard, as [Cache.close()](Cache.close.md#diskcache_oxide.Cache.close) does for one.


Usage

``` python
FanoutCache.close(
    *,
    checkpoint=True,
)
```


The whole close shares one budget: the checkpoints share at most 1 second, and behind a fork that stays stuck every release together waits at most 1 second more. It never raises. Using the cache again reopens the shards it touches.


## Parameters


`checkpoint: bool = ``True`  
Checkpoint each shard's write-ahead log first, as [Cache.close](Cache.close.md#diskcache_oxide.Cache.close) does.
