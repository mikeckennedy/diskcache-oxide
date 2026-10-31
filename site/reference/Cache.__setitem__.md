## Cache.\_\_setitem\_\_()


`cache[key] = value`: store a value that never expires, like [set()](Cache.set.md#diskcache_oxide.Cache.set). Under `durable=True` a failed write is dropped.


Usage

``` python
Cache.__setitem__(
    key,
    value,
)
```
