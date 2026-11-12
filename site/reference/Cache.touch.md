## Cache.touch()


Change the expiry of an existing entry without rewriting its value.


Usage

``` python
Cache.touch(
    key,
    expire=None,
    *,
    retry=False,
)
```


## Parameters


`key: Key`  
The key.

`expire: float | None = None`  
Seconds from now until the entry expires, or None to make it never expire.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Returns


`bool`  
True if the entry existed and was updated, False if it was missing or expired, or with

`durable=True`, if the call failed.


## Raises


`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).


## Examples

``` python
cache.touch('session:xyz', expire=1800)   # 30 more minutes, if it's still there
```
