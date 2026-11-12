## FanoutCache.expire()


Remove every expired entry from every shard, and its value file, as [Cache.expire](Cache.expire.md#diskcache_oxide.Cache.expire) does.


Usage

``` python
FanoutCache.expire(
    *,
    timeout=None,
    retry=False,
)
```


## Parameters


`timeout: float | None = None`  
Seconds the whole call may spend waiting for write locks (another thread's or another process's) or a pending fork, across every shard, for this call only (default: the cache's [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout)). It bounds waiting, not work: a chunk already running finishes, and once it's spent, each remaining chunk still gets one try with no wait, so shards no one else is writing are all done even at a short timeout.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Returns


`int`  
How many entries were removed.


## Raises


`Timeout`  
Some shard stayed locked past [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout); the others were still done. It carries `removed` and `failed_shards` (see the class docs), as any failure here does, under `durable=True` too.


## Examples

``` python
try:
    cache.expire(timeout=5.0)
except Timeout as exc:
    exc.failed_shards, exc.removed   # which shards were skipped, and how many entries went anyway
```
