## FanoutCache.checkpoint()


Checkpoint every shard's write-ahead log and truncate it to zero, as [Cache.checkpoint](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) does.


Usage

``` python
FanoutCache.checkpoint(
    *,
    timeout=None,
)
```


## Parameters


`timeout: float | None = None`  
Seconds to keep retrying busy shards, for the whole call (default: the cache's [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout)). A shard reached after it's spent still gets one try.


## Returns


`bool`  
True if every shard's log was reset, False if readers or writers in other processes, or

a pending fork, kept any of them busy.


## Raises


`OSError`  
A real error, such as a full disk, on some shard, raised after every shard was tried. It carries `failed_shards` (see the class docs).
