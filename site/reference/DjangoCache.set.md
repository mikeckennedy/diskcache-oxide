## DjangoCache.set()


Django's [set](Cache.set.md#diskcache_oxide.Cache.set), plus `tag=`: an entry tagged here can be removed with `evict(tag)`.


Usage

``` python
DjangoCache.set(
    key,
    value,
    timeout=DEFAULT_TIMEOUT,
    version=None,
    tag=None,
)
```
