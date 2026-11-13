## Caches.remove_old_formats()


Run [remove_old_formats()](Cache.remove_old_formats.md#diskcache_oxide.Cache.remove_old_formats) on every registered cache, opening each one, even while it's


Usage

``` python
Caches.remove_old_formats()
```


disabled. Returns what each removed, or None for a cache that couldn't be opened or cleaned (logged; the rest still run).

It doesn't check whether an older diskcache-oxide still uses those files: call it once no older worker is left. See [Cache.remove_old_formats()](Cache.remove_old_formats.md#diskcache_oxide.Cache.remove_old_formats).
