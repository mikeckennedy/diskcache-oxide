## Memoized


A function wrapped by [memoize](Cache.memoize.md#diskcache_oxide.Cache.memoize): called as the function was, plus [__cache_key__](Memoized.md#diskcache_oxide.Memoized.__cache_key__).


Usage

``` python
Memoized()
```


## Methods

| Name | Description |
|----|----|
| [__cache_key__()](#__cache_key__) | The cache key a call with these arguments uses, for `cache.delete(f.__cache_key__(...))`. |

------------------------------------------------------------------------


#### \_\_cache_key\_\_()


The cache key a call with these arguments uses, for `cache.delete(f.__cache_key__(...))`.


Usage

``` python
__cache_key__(*args, **kwargs)
```
