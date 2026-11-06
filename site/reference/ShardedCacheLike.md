## ShardedCacheLike


A [CacheLike](CacheLike.md#diskcache_oxide.CacheLike) with a sharded cache's `transact(*, key, retry=False)`, which [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) and


Usage

``` python
ShardedCacheLike()
```


[NullCache](NullCache.md#diskcache_oxide.NullCache) both have.

On a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache), `with cache.transact(key='total') as tx:` runs the block's calls on this thread for keys on `key`'s shard in one write transaction, and refuses keys on other shards (`ValueError`); `tx` is the cache. On a [NullCache](NullCache.md#diskcache_oxide.NullCache) it's a no-op that yields the [NullCache](NullCache.md#diskcache_oxide.NullCache). `key` is keyword-only, so `transact('total')` is a type error rather than a `retry` of 'total'.


## Methods

| Name | Description |
|----|----|
| [transact()](#transact) | A context manager holding a write transaction on `key`'s shard for the block, yielding the cache. |

------------------------------------------------------------------------


#### transact()


A context manager holding a write transaction on `key`'s shard for the block, yielding the cache.


Usage

``` python
transact(*, key, retry=False)
```
