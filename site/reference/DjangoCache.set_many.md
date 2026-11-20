## DjangoCache.set_many()


A [set](Cache.set.md#diskcache_oxide.Cache.set) per key, not one transaction: a value's pickling can run arbitrary code (load a


Usage

``` python
DjangoCache.set_many(
    data,
    timeout=DEFAULT_TIMEOUT,
    version=None,
)
```


session, run a query), which mustn't run holding the cache's write lock. Returns the keys that weren't stored, which only a `durable=True` cache can have. A durable [set](Cache.set.md#diskcache_oxide.Cache.set) behind a held lock waits out the lock wait and returns False, so once one [set](Cache.set.md#diskcache_oxide.Cache.set) returns False having itself taken half a lock wait, every key after it is returned untried: one wait, not one per key. A failure that comes at once (one value that can't be written) only fails that key, however long the batch has run. With a lock wait of 0 no [set](Cache.set.md#diskcache_oxide.Cache.set) waits, so every key is tried.
