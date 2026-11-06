## Caches.fanout()


The named sharded cache (one registered with `shards=`), looked up as `caches[name]` is.


Usage

``` python
Caches.fanout(name)
```


A [NullCache](NullCache.md#diskcache_oxide.NullCache) while it can't be opened or is disabled, exactly as for a plain cache. That includes a directory whose layout doesn't match the registration ([LayoutMismatchError](LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError): a different shard count, or a plain cache's directory): reported through `on_open_error`, remembered, and retried after `retry_open_after`. Its `transact(retry=False, *, key=None)` accepts the `key=` a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) needs, so `caches.fanout(name).transact(key=k)` runs whether the cache is on or off. Both kinds satisfy [ShardedCacheLike](ShardedCacheLike.md#diskcache_oxide.ShardedCacheLike), so code (or a test fake) can be typed against that; the return stays the union so [shard_of](FanoutCache.shard_of.md#diskcache_oxide.FanoutCache.shard_of) and [shards](FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) still type-check on it when it's a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache).


## Raises


`KeyError`  
No cache by that name is registered.

`TypeError`  
The cache is a plain one (use `caches[name]`), or its settings are invalid.

`ValueError`  
Its settings are invalid.
