## Cache.add()


Store `value` only if `key` is missing or expired.


Usage

``` python
Cache.add(
    key,
    value,
    expire=None,
    *,
    retry=False,
)
```


Atomic across threads and processes: when several callers race to add one key, exactly one succeeds. That makes it a simple way to claim a job or take a lock.


## Parameters


`key: Key`  
The key.

`value: Any`  
The value, as for [set()](Cache.set.md#diskcache_oxide.Cache.set).

`expire: float | None = None`  
Seconds until the entry expires, or None for never.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Returns


`bool`  
True if the value was stored, False if the key already held a live value. With

`durable=True`, also False if the write failed, so a claim fails closed. A [NullCache](NullCache.md#diskcache_oxide.NullCache)

returns True, so use a non-durable handle for claims and locks.


## Raises


`TypeError`  
The value can't be stored exactly without pickle, or [expire](Cache.expire.md#diskcache_oxide.Cache.expire) is a `bool`.

`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).


## Examples

``` python
if cache.add('job:hourly-cleanup', os.getpid(), expire=55 * 60):
    remove_stale_sessions()   # only the worker whose add() returned True gets here
```
