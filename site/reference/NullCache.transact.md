## NullCache.transact()


A no-op context manager that yields this [NullCache](NullCache.md#diskcache_oxide.NullCache).


Usage

``` python
NullCache.transact(
    retry=False,
    *,
    key=None,
)
```


It never opens a transaction on an `invalidate=` cache: removals forwarded inside the block land at once, and an exception in the block doesn't undo them.


## Parameters


`retry: bool = ``False`  
Accepted for [Cache.transact()](Cache.transact.md#diskcache_oxide.Cache.transact)'s signature; it must be a `bool`.

`key: Key | None = None`  
Accepted so a switched-off sharded cache's `transact(key=...)` works too; it must be a key or None.


## Raises


`TypeError`  
`retry` isn't a `bool`, or `key` isn't a key type.
