## Cache.pop()


Remove `key` and return its value, delivering it to exactly one caller.


Usage

``` python
Cache.pop(
    key,
    default=None,
)
```


Reads and decodes the value first, as [get](Cache.get.md#diskcache_oxide.Cache.get) does, then deletes exactly that stored version, so a value it can't read stays for whoever can. A [pop](Cache.pop.md#diskcache_oxide.Cache.pop) that loses that delete to other writers 8 times falls back to removing the row first and decoding afterwards. Only then can a failed decode cost the value: one from a newer version, or any error from a pickle hook. [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout) covers the whole call.


## Parameters


`key: str`  
The key.

`default: Any = None`  
Returned when the key is missing or expired, or its value file is missing.


## Returns


`Any`  
The removed value, or `default`. With `durable=True`, a [pop](Cache.pop.md#diskcache_oxide.Cache.pop) whose delete fails returns

`default`, never the value, which could otherwise be delivered twice. The row stays.


## Raises


`UnsafeValueError`  
The value was stored with pickle and this cache has `pickle=False`. The row stays.

`ForeignValueError`  
`on_decode_error='raise'`, and another version or host might read the value. The row stays.

`CorruptValueError`  
`on_decode_error='raise'`, and no build could read the value. The row is deleted first.

`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), or [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout) ran out after losing a race to another writer. This [pop](Cache.pop.md#diskcache_oxide.Cache.pop) deletes nothing, and whatever the other writer left stays.


## Examples

``` python
token = cache.pop('reset-token:abc')   # None if it was missing or already used
```
