## CacheLike.incr()


Add `delta` to the number under `key`, atomically, and return the result.


Usage

``` python
CacheLike.incr(
    key,
    delta=1,
    default=0,
    *,
    retry=False,
)
```
