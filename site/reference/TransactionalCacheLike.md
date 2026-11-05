## TransactionalCacheLike


A [CacheLike](CacheLike.md#diskcache_oxide.CacheLike) with [transact()](Cache.transact.md#diskcache_oxide.Cache.transact), which [Cache](Cache.md#diskcache_oxide.Cache) and [NullCache](NullCache.md#diskcache_oxide.NullCache) both have.


Usage

``` python
TransactionalCacheLike()
```


On a [Cache](Cache.md#diskcache_oxide.Cache), `with cache.transact() as tx:` runs the block's calls on this thread in one write transaction; `tx` is the cache. On a [NullCache](NullCache.md#diskcache_oxide.NullCache) it's a no-op that yields the [NullCache](NullCache.md#diskcache_oxide.NullCache).


## Methods

| Name | Description |
|----|----|
| [transact()](#transact) | A context manager holding a write transaction for the block, yielding the cache. |

------------------------------------------------------------------------


#### transact()


A context manager holding a write transaction for the block, yielding the cache.


Usage

``` python
transact(retry=False)
```
