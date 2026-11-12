## FanoutCache.stats()


`(hits, misses)` of [get](Cache.get.md#diskcache_oxide.Cache.get) and `cache[key]` in this process, as diskcache's [stats()](Cache.stats.md#diskcache_oxide.Cache.stats).


Usage

``` python
FanoutCache.stats(
    enable=True,
    reset=False,
)
```


Returns the counts as they were, then resets them if [reset](Cache.reset.md#diskcache_oxide.Cache.reset), then turns counting on or off with [enable](Caches.enable.md#diskcache_oxide.Caches.enable) (off by default). Counted per process and in memory, never in the database, so a read never writes: other processes' lookups aren't included, the counts start from zero when the process (or a forked child) starts, and every handle on one directory in a process shares them. `in`, [pop](Cache.pop.md#diskcache_oxide.Cache.pop) and iteration don't count; a durable outage answered as a miss counts as nothing. A [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) sums its shards.
