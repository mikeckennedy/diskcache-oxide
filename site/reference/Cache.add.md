## Cache.add()


Store `value` only if `key` is missing or expired.


Usage

``` python
Cache.add(
    key,
    value,
    expire=None,
)
```


Atomic across threads and processes: when several callers race to add one key, exactly one succeeds. That makes it a simple way to claim a job or take a lock.


## Parameters


`key: str`  
The key.

`value: Any`  
The value, as for [set()](Cache.set.md#diskcache_oxide.Cache.set).

`expire: float | None = None`  
Seconds until the entry expires, or None for never.


## Returns


`bool`  
True if the value was stored, False if the key already held a live value.


## Raises


`TypeError`  
The value can't be stored exactly without pickle, or [expire](Cache.expire.md#diskcache_oxide.Cache.expire) is a `bool`.

`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).
