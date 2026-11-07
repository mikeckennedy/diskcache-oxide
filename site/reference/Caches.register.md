## Caches.register()


Register a cache. Nothing opens until its first lookup.


Usage

``` python
Caches.register(
    name,
    *,
    directory=None,
    **settings,
)
```


## Parameters


`name: N`  
The cache's name. Without [directory](Cache.directory.md#diskcache_oxide.Cache.directory), it's also the directory's name under `base_dir`, so it must be a plain name: no path separators, not `.` or `..`.

`directory: str | os.PathLike[str] | None = None`  
Put this cache somewhere else.

`**settings: Any`  
[Cache](Cache.md#diskcache_oxide.Cache) settings for this cache, over the registry's defaults. A callable `on_decode_error` is called as `(name, key, exc)`, as at the registry level. Adding `shards=` registers a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) instead, which takes the same settings; read it with `fanout(name)`. `shards=n` is the count for a new folder: an existing one keeps the count it was created with, and the registry warns when that isn't `n`. Passing the key at all is what picks the kind, so `shards=None` is NOT "not sharded": it is a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) with the stored shard count, or 8 for a new root. Leave [shards](FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) out for a plain cache (a config file that writes `shards: null` for "none" would register a FanoutCache).


## Raises


`ValueError`  
The name is taken or isn't a plain directory name, or another registered cache already uses that directory (two names would share one store, and the last one opened would quietly set its [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit)).

`TypeError`  
An unknown [Cache](Cache.md#diskcache_oxide.Cache) (or [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache)) setting.


## Examples

``` python
caches.register('pages', size_limit=2 * 2**30)      # /var/cache/my-app/pages
caches.register('objects', pickle=True, directory='/mnt/fast/objects')
caches.register('rate_limits', shards=8)            # a FanoutCache; read with fanout()
```
