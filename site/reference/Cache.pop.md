## Cache.pop()


Remove `key` and return its value, atomically.


Usage

``` python
Cache.pop(
    key,
    default=None,
)
```


## Parameters


`key: str`  
The key.

`default: Any = None`  
Returned when the key is missing or expired.


## Returns


`Any`  
The removed value, or `default`.


## Raises


`UnsafeValueError`  
The value was stored with pickle and this cache has `pickle=False`.

`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).
