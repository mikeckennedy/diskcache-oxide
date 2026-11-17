## Cache.\_\_iter\_\_()


Iterate over keys in insertion order. An overwrite keeps its key's place. The walk covers


Usage

``` python
Cache.__iter__()
```


the entries there when it began: one stored meanwhile isn't yielded, and one deleted and stored again is yielded once, as in diskcache.

As with [iterkeys()](Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys), expired entries are skipped, and `next()` raises `ValueError` while another thread is inside it.
