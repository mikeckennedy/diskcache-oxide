## Cache.set()


Store `value` under `key`, replacing any existing value.


Usage

``` python
Cache.set(
    key,
    value,
    expire=None,
    *,
    retry=False,
)
```


Values of 64 KiB or more (after encoding) are stored in their own file.


## Parameters


`key: Key`  
The key.

`value: Any`  
The value: `str`, `bytes`, numbers, `Decimal`, `bool`, `None`, and lists, tuples, dicts, sets, frozensets, datetimes and dates of those, or anything picklable with `pickle=True`.

`expire: float | None = None`  
Seconds until the entry expires, or None for never.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Returns


`bool`  
True. With `durable=True`, False if the write failed and was dropped: whatever was

stored under `key` stays, and can be served stale.


## Raises


`TypeError`  
The value can't be stored exactly without pickle, or [expire](Cache.expire.md#diskcache_oxide.Cache.expire) is a `bool`.

`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).


## Examples

``` python
cache.set('episodes:popular', episodes, expire=300)   # expires in 5 minutes
cache.set('config', {'retries': 3})                   # never expires
```
