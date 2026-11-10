## Cache.get()


Return the value stored for `key`, or `default` if it's missing or expired.


Usage

``` python
Cache.get(
    key,
    default=None,
    *,
    expire_time=False,
    retry=False,
)
```


Reads never write, so any number of threads and processes can read at once.


## Parameters


`key: Key`  
The key to look up.

`default: Any = None`  
Returned when the key is missing or expired.

`expire_time: bool = ``False`  
Also return the entry's expiry time.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Returns


`Any | tuple[Any, float | None]`  
The value, or `default`. With `expire_time=True`, a `(value, expire_time)` tuple, where

`expire_time` is a Unix timestamp, or None for an entry that never expires (or a miss).


## Raises


`UnsafeValueError`  
The value was stored with pickle and this cache has `pickle=False`.

`CorruptValueError`  
The stored bytes can't be decoded.

`Timeout`  
Reading a large value kept losing to concurrent overwrites until [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), or a pending fork outlasted it. `durable=True` returns `default` instead.


## Examples

``` python
user = cache.get('user:42')                     # None on a miss
user = cache.get('user:42', default={})         # {} on a miss
value, expires_at = cache.get('user:42', expire_time=True)
```
