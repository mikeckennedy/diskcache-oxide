## CacheLike


What application code can rely on from a [Cache](Cache.md#diskcache_oxide.Cache), a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) or a [NullCache](NullCache.md#diskcache_oxide.NullCache).


Usage

``` python
CacheLike()
```


All three satisfy it, so a factory can return either and callers never need to check which one they got. A test keeps the three signatures identical.
