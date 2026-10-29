## Cache.set()


Store `value` under `key`, replacing any existing value.


Usage

``` python
Cache.set(
    key,
    value,
    expire=None,
)
```


Values of 64 KiB or more (after encoding) are stored in their own file.


## Parameters


`key: str`  
The key.

`value: Any`  
The value: `str`, `bytes`, numbers, `bool`, `None`, and lists, tuples, dicts, sets and datetimes of those, or anything picklable with `pickle=True`.

`expire: float | None = None`  
Seconds until the entry expires, or None for never.


## Returns


`bool`  
True.


## Raises


`TypeError`  
The value can't be stored exactly without pickle, or [expire](Cache.expire.md#diskcache_oxide.Cache.expire) is a `bool`.

`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).
