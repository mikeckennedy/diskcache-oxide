## FanoutCache.\_\_iter\_\_()


Iterate over every key once: shard 0's in insertion order, then shard 1's, and so on.


Usage

``` python
FanoutCache.__iter__()
```


That isn't the order keys were stored in across the cache. Each shard's walk covers the entries there when the walk reached it, as [Cache.__iter__](Cache.__iter__.md#diskcache_oxide.Cache.__iter__) does. Otherwise as [iterkeys()](Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys).
