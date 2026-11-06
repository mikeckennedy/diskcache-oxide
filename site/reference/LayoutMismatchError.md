## LayoutMismatchError


The directory's layout doesn't match how it was opened: a plain [Cache](Cache.md#diskcache_oxide.Cache) on a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) root or shard, a


Usage

``` python
LayoutMismatchError()
```


[FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) on a plain [Cache](Cache.md#diskcache_oxide.Cache)'s directory, a shard opened as another index or count than it records, or a `shards=` that differs from the stored count. Also a `ValueError`, which is what these raised before.
