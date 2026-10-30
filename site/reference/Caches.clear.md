## Caches.clear()


Clear the real cache, even while it's disabled. Returns how many entries were removed.


Usage

``` python
Caches.clear(
    name,
    *,
    timeout=None,
)
```


## Raises


`KeyError`  
No cache by that name is registered.

`(OSError, RuntimeError)`  
It can't be opened (an admin action shouldn't report 0 for that), or [Timeout](Timeout.md#diskcache_oxide.Timeout) from the clear itself.
