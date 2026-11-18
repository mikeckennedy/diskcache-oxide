## DjangoCache.delete_many()


Delete the keys, up to `DELETE_CHUNK` at a time in one transaction (one per shard on a


Usage

``` python
DjangoCache.delete_many(
    keys,
    version=None,
)
```


[FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache); a one-key chunk is a plain delete). Every key is made and validated first, so a `CacheKeyWarning` raised as an error deletes nothing. A [Timeout](Timeout.md#diskcache_oxide.Timeout) part-way leaves the chunks before it deleted, as a loop of deletes would. Inside the caller's own transaction on one shard of a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache), keys on another shard raise the engine's `ValueError`.

Under `durable=True`, single deletes, which durable covers and a transaction doesn't. A durable delete that can't get the lock returns False after the lock wait, as a miss does at once, so the batch stops at the first False past half a lock wait: one wait, not one per key.
