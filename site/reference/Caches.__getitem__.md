## Caches.\_\_getitem\_\_()


The named plain cache: opened on first use, then the same object every time.


Usage

``` python
Caches.__getitem__(name)
```


A [NullCache](NullCache.md#diskcache_oxide.NullCache) while it can't be opened (see the class docs), or `NullCache(invalidate=real)` while it's disabled. The type is [TransactionalCacheLike](TransactionalCacheLike.md#diskcache_oxide.TransactionalCacheLike), so `caches[name].transact()` checks; a cache registered with `shards=` is read with [fanout()](Caches.fanout.md#diskcache_oxide.Caches.fanout), whose [transact](Cache.transact.md#diskcache_oxide.Cache.transact) takes a key.


## Raises


`KeyError`  
No cache by that name is registered.

`TypeError`  
The cache was registered with `shards=` (use `fanout(name)`), or its settings are invalid.

`ValueError`  
Its settings are invalid.
