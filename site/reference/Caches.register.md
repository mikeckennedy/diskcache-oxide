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
[Cache](Cache.md#diskcache_oxide.Cache) settings for this cache, over the registry's defaults. A callable `on_decode_error` is called as `(name, key, exc)`, as at the registry level.


## Raises


`ValueError`  
The name is taken or isn't a plain directory name, or another registered cache already uses that directory (two names would share one store, and the last one opened would quietly set its [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit)).

`TypeError`  
An unknown [Cache](Cache.md#diskcache_oxide.Cache) setting.
