## Caches


A registry of named caches under one base directory.


Usage

``` python
Caches(
    base_dir,
    *,
    on_open_error=None,
    retry_open_after=10.0,
    _monotonic=time.monotonic,
    **defaults
)
```


`Caches[Literal['general', 'show_pages']]` makes a misspelled name a type error.


## Parameters


`base_dir: str | os.PathLike[str]`  
Where the caches live: each one in `base_dir/<name>` unless [register](Caches.register.md#diskcache_oxide.Caches.register) says otherwise. Created when the first cache opens.

`on_open_error: Callable[[str, Exception], object] | None = None`  
Called as `on_open_error(name, exc)` when a cache can't open (default: log at ERROR on the `diskcache_oxide` logger, or at WARNING with no traceback for a [Timeout](Timeout.md#diskcache_oxide.Timeout)). Whatever it raises is logged.

`retry_open_after: float = ``10.0`  
Seconds before a cache that failed to open is tried again (default 10). Lookups in between get a [NullCache](NullCache.md#diskcache_oxide.NullCache) without trying. A [Timeout](Timeout.md#diskcache_oxide.Timeout) means busy rather than broken, so that's tried again after at most 1 s.

`**defaults: Any`  
[Cache](Cache.md#diskcache_oxide.Cache) settings for every cache, such as `timeout=1.0`, `durable=True` or `on_decode_error='miss'`. [register](Caches.register.md#diskcache_oxide.Caches.register) can override them. A callable `on_decode_error` here is called as `(name, key, exc)`, so one reporter can tell the caches apart.
