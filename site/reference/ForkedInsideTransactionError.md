## ForkedInsideTransactionError


This process can't use the cache: it's the child of a fork that happened while a connection to


Usage

``` python
ForkedInsideTransactionError()
```


the cache was open, inside a [transact()](Cache.transact.md#diskcache_oxide.Cache.transact) block or from a hook running inside a cache call. Every call on that cache raises this in the child, reads included, for the life of the process, on a `durable=True` handle too, and so does leaving the inherited [transact()](Cache.transact.md#diskcache_oxide.Cache.transact) block. The parent is unaffected. Fork outside the transaction, or start the process with spawn or forkserver (D-020).
