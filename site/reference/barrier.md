## barrier()


A decorator running the function under a lock made by `lock_factory(cache, key, expire=...)`


Usage

``` python
barrier(
    cache,
    lock_factory,
    name=None,
    expire=None,
    tag=None,
)
```


([Lock](Lock.md#diskcache_oxide.Lock), [RLock](RLock.md#diskcache_oxide.RLock), [BoundedSemaphore](BoundedSemaphore.md#diskcache_oxide.BoundedSemaphore), or one of your own): one caller at a time, everywhere.
