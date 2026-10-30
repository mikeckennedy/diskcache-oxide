## Cache.cull()


Remove expired entries, then evict the least recently stored until under [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit).


Usage

``` python
Cache.cull(
    *,
    timeout=None,
)
```


## Parameters


`timeout: float | None = None`  
Seconds each chunk may wait for the write lock (another thread's or another process's), for this call only (default: the cache's [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout)). It bounds each wait, not the whole call.


## Returns


`int`  
How many entries were removed.
