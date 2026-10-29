## Cache.delete()


Remove `key`.


Usage

``` python
Cache.delete(key)
```


## Returns


`bool`  
True if a live entry was removed, False if the key was missing or expired.


## Raises


`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).
