## Cache.evict()


Remove every entry tagged `tag` that existed when the call started.


Usage

``` python
Cache.evict(
    tag,
    *,
    timeout=None,
    retry=False,
)
```


Works in chunks as [clear](Cache.clear.md#diskcache_oxide.Cache.clear) does, and keeps writes that land while it runs. Tags compare by exact type, as keys do: `evict(1)` leaves entries tagged `1.0` or `True` (D-052). `evict(None)` removes nothing: untagged entries have no tag to evict by.


## Parameters


`tag: Key`  
The tag.

`timeout: float | None = None`  
Seconds each chunk may wait for the write lock (another thread's or another process's), for this call only (default: the cache's [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout)). It bounds each wait, not the whole call.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Returns


`int`  
How many entries were removed.


## Raises


`TypeError`  
`tag` isn't a key type.


## Examples

``` python
cache.set('user:42:profile', profile, tag='user:42')
cache.set('user:42:orders', orders, tag='user:42')
cache.evict('user:42')   # 2
```
