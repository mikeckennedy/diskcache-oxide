## Cache.clear()


Remove every entry that existed when the call started.


Usage

``` python
Cache.clear(
    *,
    timeout=None,
)
```


Works in chunks and lets other writers in between them. Writes that land while it runs, including overwrites of existing keys, are kept.


## Parameters


`timeout: float | None = None`  
Seconds each chunk may wait for the write lock (another thread's or another process's), for this call only (default: the cache's [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout)). It bounds each wait, not the whole call.


## Returns


`int`  
How many entries were removed.


## Examples

``` python
cache.clear(timeout=10.0)   # safe while other processes keep writing
```
