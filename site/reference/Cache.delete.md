## Cache.delete()


Remove `key`.


Usage

``` python
Cache.delete(key)
```


## Returns


`bool`  
True if a live entry was removed, False if the key was missing or expired. With

`durable=True`, also False if the delete failed: that invalidation is lost, and the

entry can be served stale until it expires, or forever if it has no expiry. The log

line is the only signal.


## Raises


`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).


## Examples

``` python
cache.delete('user:42')        # True if it was there
for key in cache.iterkeys(prefix='user:42:'):
    cache.delete(key)          # safe while iterating
```
