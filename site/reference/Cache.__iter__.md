## Cache.\_\_iter\_\_()


Iterate over keys in insertion order. An overwrite keeps its key's place.


Usage

``` python
Cache.__iter__()
```


As with [iterkeys()](Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys), expired entries are skipped, and `next()` raises `ValueError` while another thread is inside it.
