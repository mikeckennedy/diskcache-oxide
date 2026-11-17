## MaintenanceStats


How background maintenance has gone on a cache in this process: [maintenance_stats()](Cache.maintenance_stats.md#diskcache_oxide.Cache.maintenance_stats).


Usage

``` python
MaintenanceStats
```


`interval` is in seconds, 0 when maintenance is off for this cache here (`maintenance_interval=0`, or a handle with its own `clock`). `turns` and `removed` count the turns that ran and the entries they removed; `failures` counts turns that failed, plus one for each stretch of 3 intervals in which turns found work and couldn't get to it (another connection kept the write lock). `last_error` is the last failure's message. On a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache), the counts are every shard's summed, `interval` is the largest, and `last_error` the first shard's that has one.
