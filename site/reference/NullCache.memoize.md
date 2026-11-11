## NullCache.memoize()


As [Cache.memoize](Cache.memoize.md#diskcache_oxide.Cache.memoize), but every call runs the function, as every entry has already expired. The


Usage

``` python
NullCache.memoize(
    name=None,
    typed=False,
    expire=None,
    tag=None,
    ignore=(),
)
```


arguments are still checked as keys, so a call that would fail on a real cache fails here.
