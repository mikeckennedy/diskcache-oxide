## Cache.maintenance_stats()


How background maintenance has gone on this cache in this process (see


Usage

``` python
Cache.maintenance_stats()
```


`maintenance_interval`): its interval in seconds (0 when off here, or closed), the turns that ran, the entries they removed, failures, and the last failure's message. A failure is also logged once per streak, as a WARNING on the `diskcache_oxide` logger, by the next single-key call on the cache ([get](Cache.get.md#diskcache_oxide.Cache.get), [set](Cache.set.md#diskcache_oxide.Cache.set), [incr](Cache.incr.md#diskcache_oxide.Cache.incr) and the like); a cache nobody calls shows it here only. A thread that couldn't be started counts as failures too. On a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache), every shard's counts summed, the largest interval, and the first shard's last error.
