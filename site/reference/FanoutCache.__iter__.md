## FanoutCache.\_\_iter\_\_()


Iterate over every key once: shard 0's in insertion order, then shard 1's, and so on.


Usage

``` python
FanoutCache.__iter__()
```


That isn't the order keys were stored in across the cache. Otherwise as [iterkeys()](Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys).
