## Cache.touch()


Change the expiry of an existing entry without rewriting its value.


Usage

``` python
Cache.touch(
    key,
    expire=None,
)
```


## Parameters


`key: str`  
The key.

`expire: float | None = None`  
Seconds from now until the entry expires, or None to make it never expire.


## Returns


`bool`  
True if the entry existed and was updated, False if it was missing or expired, or with

`durable=True`, if the call failed.


## Raises


`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).
