## CacheLike.add()


Store `value` only if `key` is missing or expired. True if it was stored.


Usage

``` python
CacheLike.add(
    key,
    value,
    expire=None,
    *,
    retry=False,
)
```
