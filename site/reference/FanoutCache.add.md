## FanoutCache.add()


As [Cache.add](Cache.add.md#diskcache_oxide.Cache.add), on the key's shard: atomic across threads and processes.


Usage

``` python
FanoutCache.add(
    key,
    value,
    expire=None,
    *,
    retry=False,
)
```
