## Cache.expire()


Remove every expired entry, and its value file.


Usage

``` python
Cache.expire(
    *,
    timeout=None,
    retry=False,
)
```


Works in chunks and lets other writers in between them.


## Parameters


`timeout: float | None = None`  
Seconds each chunk may wait for the write lock (another thread's or another process's), for this call only (default: the cache's [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout)). It bounds each wait, not the whole call.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Returns


`int`  
How many entries were removed.


## Examples

``` python
removed = cache.expire(timeout=10.0)   # from a scheduled job, with its own timeout
```
