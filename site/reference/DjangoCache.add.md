## DjangoCache.add()


Django's [add](Cache.add.md#diskcache_oxide.Cache.add), plus `tag=`: an entry tagged here can be removed with `evict(tag)`.


Usage

``` python
DjangoCache.add(
    key,
    value,
    timeout=DEFAULT_TIMEOUT,
    version=None,
    tag=None,
)
```
