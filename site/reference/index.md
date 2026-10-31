# API Reference


The public API of diskcache-oxide: Cache, NullCache, CacheLike protocol, the Caches registry, exceptions, and SQLite utilities.


## Cache


Disk-backed cache backed by SQLite, shared safely across threads and processes.


[Cache](Cache.md#diskcache_oxide.Cache)  
A disk cache backed by SQLite, shared safely across threads and processes.


## Cache Methods


Methods for the Cache class


[Cache.directory](Cache.directory.md#diskcache_oxide.Cache.directory)  
The cache directory, as given to the constructor.

[Cache.timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout)  
Seconds a call waits for a lock or a pending fork before raising `Timeout`.

[Cache.size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit)  
The disk budget in bytes. A `set()` that finds the cache over it evicts entries.

[Cache.cull_limit](Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit)  
The most entries a single `set()` evicts when the cache is over `size_limit`.

[Cache.enabled](Cache.enabled.md#diskcache_oxide.Cache.enabled)  
Always True for a real cache; False for a `NullCache`.

[Cache.set()](Cache.set.md#diskcache_oxide.Cache.set)  
Store `value` under `key`, replacing any existing value.

[Cache.add()](Cache.add.md#diskcache_oxide.Cache.add)  
Store `value` only if `key` is missing or expired.

[Cache.delete()](Cache.delete.md#diskcache_oxide.Cache.delete)  
Remove `key`.

[Cache.pop()](Cache.pop.md#diskcache_oxide.Cache.pop)  
Remove `key` and return its value, delivering it to exactly one caller.

[Cache.touch()](Cache.touch.md#diskcache_oxide.Cache.touch)  
Change the expiry of an existing entry without rewriting its value.

[Cache.incr()](Cache.incr.md#diskcache_oxide.Cache.incr)  
Add `delta` to the number stored under `key`, atomically, and return the result.

[Cache.decr()](Cache.decr.md#diskcache_oxide.Cache.decr)  
Subtract `delta` from the number stored under `key`, atomically. See `incr()`.

[Cache.iterkeys()](Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys)  
Iterate over keys in sorted order, optionally only those starting with `prefix`.

[Cache.expire()](Cache.expire.md#diskcache_oxide.Cache.expire)  
Remove every expired entry, and its value file.

[Cache.clear()](Cache.clear.md#diskcache_oxide.Cache.clear)  
Remove every entry that existed when the call started.

[Cache.cull()](Cache.cull.md#diskcache_oxide.Cache.cull)  
Remove expired entries, then evict the least recently stored until under `size_limit`.

[Cache.volume()](Cache.volume.md#diskcache_oxide.Cache.volume)  
The cache's size on disk in bytes: the database plus value files.

[Cache.checkpoint()](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint)  
Copy the write-ahead log into the database and truncate it to zero.

[Cache.close()](Cache.close.md#diskcache_oxide.Cache.close)  
Close this handle and return its connections.

[Cache.__getitem__()](Cache.__getitem__.md#diskcache_oxide.Cache.__getitem__)  
`cache[key]`: the value, or `KeyError` if it's missing or expired, or the read failed under `durable=True`.

[Cache.__setitem__()](Cache.__setitem__.md#diskcache_oxide.Cache.__setitem__)  
`cache[key] = value`: store a value that never expires, like `set()`. Under `durable=True` a failed write is dropped.

[Cache.__delitem__()](Cache.__delitem__.md#diskcache_oxide.Cache.__delitem__)  
`del cache[key]`: remove the entry, or raise `KeyError` if it's missing or expired, or the delete failed under `durable=True`.

[Cache.__contains__()](Cache.__contains__.md#diskcache_oxide.Cache.__contains__)  
`key in cache`: True if the key holds a live (unexpired) value. False if the check failed under `durable=True`.

[Cache.__len__()](Cache.__len__.md#diskcache_oxide.Cache.__len__)  
The number of entries, including expired entries not yet removed.

[Cache.__bool__()](Cache.__bool__.md#diskcache_oxide.Cache.__bool__)  
Always True, even when empty, so `if cache:` means "caching is on".

[Cache.__iter__()](Cache.__iter__.md#diskcache_oxide.Cache.__iter__)  
Iterate over keys in insertion order. An overwrite keeps its key's place.

[Cache.__enter__()](Cache.__enter__.md#diskcache_oxide.Cache.__enter__)  
Return the cache, for `with Cache(...) as cache:`.

[Cache.__exit__()](Cache.__exit__.md#diskcache_oxide.Cache.__exit__)  
Close the cache, as `close()` does.

[Cache.get()](Cache.get.md#diskcache_oxide.Cache.get)  
Return the value stored for `key`, or `default` if it's missing or expired.


## NullCache


Switched-off cache behaving as if every entry expires instantly.


[NullCache](NullCache.md#diskcache_oxide.NullCache)  
A switched-off cache, so app code needs no `if cache is None` checks.

[CacheLike](CacheLike.md#diskcache_oxide.CacheLike)  
What application code can rely on from a `Cache` or a `NullCache`.


## NullCache Methods


Methods for the NullCache class


[NullCache.__repr__()](NullCache.__repr__.md#diskcache_oxide.NullCache.__repr__)  
Shows the directory and any `invalidate=` cache.

[NullCache.directory](NullCache.directory.md#diskcache_oxide.NullCache.directory)  
The directory passed to the constructor, or None. Nothing is created there.

[NullCache.timeout](NullCache.timeout.md#diskcache_oxide.NullCache.timeout)  
The timeout passed to the constructor (default 60). Unused.

[NullCache.size_limit](NullCache.size_limit.md#diskcache_oxide.NullCache.size_limit)  
The size limit passed to the constructor (default 1 GiB). Unused.

[NullCache.cull_limit](NullCache.cull_limit.md#diskcache_oxide.NullCache.cull_limit)  
The cull limit passed to the constructor (default 10). Unused.

[NullCache.enabled](NullCache.enabled.md#diskcache_oxide.NullCache.enabled)  
Always False: caching is off.

[NullCache.get()](NullCache.get.md#diskcache_oxide.NullCache.get)  
Return `default`, as every entry has already expired. With `expire_time=True`, `(default, None)`.

[NullCache.__getitem__()](NullCache.__getitem__.md#diskcache_oxide.NullCache.__getitem__)  
Always raises `KeyError`, as every entry has already expired.

[NullCache.__contains__()](NullCache.__contains__.md#diskcache_oxide.NullCache.__contains__)  
Always False.

[NullCache.__len__()](NullCache.__len__.md#diskcache_oxide.NullCache.__len__)  
Always 0.

[NullCache.__bool__()](NullCache.__bool__.md#diskcache_oxide.NullCache.__bool__)  
Always False, so `if cache:` means "caching is on".

[NullCache.__iter__()](NullCache.__iter__.md#diskcache_oxide.NullCache.__iter__)  
Yields nothing.

[NullCache.volume()](NullCache.volume.md#diskcache_oxide.NullCache.volume)  
Always 0: a `NullCache` uses no disk.

[NullCache.checkpoint()](NullCache.checkpoint.md#diskcache_oxide.NullCache.checkpoint)  
Always True: there is no write-ahead log to reset.

[NullCache.set()](NullCache.set.md#diskcache_oxide.NullCache.set)  
Return True without storing anything: the write succeeds, then expires instantly.

[NullCache.__setitem__()](NullCache.__setitem__.md#diskcache_oxide.NullCache.__setitem__)  
Store nothing, as `set()` does.

[NullCache.add()](NullCache.add.md#diskcache_oxide.NullCache.add)  
Return True without storing anything: the key is always absent, so the add succeeds.

[NullCache.touch()](NullCache.touch.md#diskcache_oxide.NullCache.touch)  
Always False: the key never exists.

[NullCache.incr()](NullCache.incr.md#diskcache_oxide.NullCache.incr)  
Return `default + delta`, since every count starts again from `default`.

[NullCache.decr()](NullCache.decr.md#diskcache_oxide.NullCache.decr)  
Return `default - delta`. See `incr()`.

[NullCache.delete()](NullCache.delete.md#diskcache_oxide.NullCache.delete)  
False, or with `invalidate=`, delete the key from that cache and return its result.

[NullCache.__delitem__()](NullCache.__delitem__.md#diskcache_oxide.NullCache.__delitem__)  
Raise `KeyError`, or with `invalidate=`, delete the key from that cache.

[NullCache.pop()](NullCache.pop.md#diskcache_oxide.NullCache.pop)  
Return `default`. With `invalidate=`, also delete the key from that cache.

[NullCache.clear()](NullCache.clear.md#diskcache_oxide.NullCache.clear)  
0, or with `invalidate=`, clear that cache (passing `timeout` on) and return its count.

[NullCache.iterkeys()](NullCache.iterkeys.md#diskcache_oxide.NullCache.iterkeys)  
Yield nothing, or with `invalidate=`, that cache's keys, so list-then-delete still works.

[NullCache.expire()](NullCache.expire.md#diskcache_oxide.NullCache.expire)  
Always 0.

[NullCache.cull()](NullCache.cull.md#diskcache_oxide.NullCache.cull)  
Always 0.

[NullCache.close()](NullCache.close.md#diskcache_oxide.NullCache.close)  
Do nothing. An `invalidate=` cache is never closed: it belongs to whoever created it.

[NullCache.__enter__()](NullCache.__enter__.md#diskcache_oxide.NullCache.__enter__)  
Return the cache, for `with NullCache() as cache:`.

[NullCache.__exit__()](NullCache.__exit__.md#diskcache_oxide.NullCache.__exit__)  
Do nothing, as `close()` does.


## CacheLike Methods


Methods for the CacheLike class


[CacheLike.directory](CacheLike.directory.md#diskcache_oxide.CacheLike.directory)  
The cache directory (None for a `NullCache` created without one).

[CacheLike.timeout](CacheLike.timeout.md#diskcache_oxide.CacheLike.timeout)  
Seconds a write waits for another process's lock before raising `Timeout`.

[CacheLike.size_limit](CacheLike.size_limit.md#diskcache_oxide.CacheLike.size_limit)  
The disk budget in bytes.

[CacheLike.cull_limit](CacheLike.cull_limit.md#diskcache_oxide.CacheLike.cull_limit)  
The most entries one `set()` evicts when over `size_limit`.

[CacheLike.enabled](CacheLike.enabled.md#diskcache_oxide.CacheLike.enabled)  
True if caching is on (`Cache`), False if it's off (`NullCache`).

[CacheLike.get()](CacheLike.get.md#diskcache_oxide.CacheLike.get)  
Return the value for `key`, or `default` if it's missing or expired.

[CacheLike.set()](CacheLike.set.md#diskcache_oxide.CacheLike.set)  
Store `value` under `key`, with an optional expiry in seconds.

[CacheLike.add()](CacheLike.add.md#diskcache_oxide.CacheLike.add)  
Store `value` only if `key` is missing or expired. True if it was stored.

[CacheLike.delete()](CacheLike.delete.md#diskcache_oxide.CacheLike.delete)  
Remove `key`. True if a live entry was removed.

[CacheLike.pop()](CacheLike.pop.md#diskcache_oxide.CacheLike.pop)  
Remove `key` and return its value, or `default`.

[CacheLike.touch()](CacheLike.touch.md#diskcache_oxide.CacheLike.touch)  
Change an existing entry's expiry. True if the entry existed.

[CacheLike.incr()](CacheLike.incr.md#diskcache_oxide.CacheLike.incr)  
Add `delta` to the number under `key`, atomically, and return the result.

[CacheLike.decr()](CacheLike.decr.md#diskcache_oxide.CacheLike.decr)  
Subtract `delta` from the number under `key`, atomically, and return the result.

[CacheLike.iterkeys()](CacheLike.iterkeys.md#diskcache_oxide.CacheLike.iterkeys)  
Iterate over keys in sorted order, optionally only those starting with `prefix`.

[CacheLike.expire()](CacheLike.expire.md#diskcache_oxide.CacheLike.expire)  
Remove every expired entry. Returns how many were removed.

[CacheLike.clear()](CacheLike.clear.md#diskcache_oxide.CacheLike.clear)  
Remove every entry that existed when the call started. Returns how many.

[CacheLike.cull()](CacheLike.cull.md#diskcache_oxide.CacheLike.cull)  
Remove expired entries, then evict until under `size_limit`. Returns how many.

[CacheLike.volume()](CacheLike.volume.md#diskcache_oxide.CacheLike.volume)  
The size on disk in bytes.

[CacheLike.checkpoint()](CacheLike.checkpoint.md#diskcache_oxide.CacheLike.checkpoint)  
Empty the write-ahead log. True if it was reset, False if other processes kept it busy.

[CacheLike.close()](CacheLike.close.md#diskcache_oxide.CacheLike.close)  
Close this handle and return its connections.

[CacheLike.__getitem__()](CacheLike.__getitem__.md#diskcache_oxide.CacheLike.__getitem__)  
`cache[key]`: the value, or `KeyError` if it's missing or expired.

[CacheLike.__setitem__()](CacheLike.__setitem__.md#diskcache_oxide.CacheLike.__setitem__)  
`cache[key] = value`: store a value that never expires.

[CacheLike.__delitem__()](CacheLike.__delitem__.md#diskcache_oxide.CacheLike.__delitem__)  
`del cache[key]`: remove the entry, or raise `KeyError`.

[CacheLike.__contains__()](CacheLike.__contains__.md#diskcache_oxide.CacheLike.__contains__)  
`key in cache`: True if the key holds a live value.

[CacheLike.__len__()](CacheLike.__len__.md#diskcache_oxide.CacheLike.__len__)  
The number of entries.

[CacheLike.__bool__()](CacheLike.__bool__.md#diskcache_oxide.CacheLike.__bool__)  
True if caching is on, so `if cache:` reads naturally.

[CacheLike.__iter__()](CacheLike.__iter__.md#diskcache_oxide.CacheLike.__iter__)  
Iterate over keys in insertion order.

[CacheLike.__enter__()](CacheLike.__enter__.md#diskcache_oxide.CacheLike.__enter__)  
Return the cache, for use in a `with` block.

[CacheLike.__exit__()](CacheLike.__exit__.md#diskcache_oxide.CacheLike.__exit__)  
Close the cache.


## Caches


A registry of named caches under one base directory.


[Caches](Caches.md#diskcache_oxide.Caches)  
A registry of named caches under one base directory.

[CacheStats](CacheStats.md#diskcache_oxide.CacheStats)  
One cache's numbers, for an admin page. `error` says why it couldn't be read, if it couldn't.


## Caches Methods


Methods for the Caches class


[Caches.__repr__()](Caches.__repr__.md#diskcache_oxide.Caches.__repr__)  

[Caches.register()](Caches.register.md#diskcache_oxide.Caches.register)  
Register a cache. Nothing opens until its first lookup.

[Caches.__contains__()](Caches.__contains__.md#diskcache_oxide.Caches.__contains__)  

[Caches.__iter__()](Caches.__iter__.md#diskcache_oxide.Caches.__iter__)  
The registered names, in registration order.

[Caches.__len__()](Caches.__len__.md#diskcache_oxide.Caches.__len__)  

[Caches.directory()](Caches.directory.md#diskcache_oxide.Caches.directory)  
Where the named cache lives.

[Caches.__getitem__()](Caches.__getitem__.md#diskcache_oxide.Caches.__getitem__)  
The named cache: opened on first use, then the same object every time.

[Caches.disable()](Caches.disable.md#diskcache_oxide.Caches.disable)  
Later lookups of these caches return `NullCache(invalidate=real)`.

[Caches.enable()](Caches.enable.md#diskcache_oxide.Caches.enable)  
Later lookups of these caches return the real cache again.

[Caches.enabled()](Caches.enabled.md#diskcache_oxide.Caches.enabled)  
Whether lookups of this cache return the real one (it may still fail to open).

[Caches.stats()](Caches.stats.md#diskcache_oxide.Caches.stats)  
Count and volume for every registered cache, opening each one. Never raises for a cache

[Caches.clear()](Caches.clear.md#diskcache_oxide.Caches.clear)  
Clear the real cache, even while it's disabled. Returns how many entries were removed.

[Caches.expire_all()](Caches.expire_all.md#diskcache_oxide.Caches.expire_all)  
Remove expired entries from every cache. Returns how many each removed, or None for a

[Caches.close_all()](Caches.close_all.md#diskcache_oxide.Caches.close_all)  
Close every opened cache and forget it; a later lookup opens it again.

[Caches.__enter__()](Caches.__enter__.md#diskcache_oxide.Caches.__enter__)  

[Caches.__exit__()](Caches.__exit__.md#diskcache_oxide.Caches.__exit__)  


## Errors


Exceptions raised by cache operations.


[Timeout](Timeout.md#diskcache_oxide.Timeout)  
A lock stayed held, or a fork stayed pending, for longer than the cache's timeout.

[UnsafeValueError](UnsafeValueError.md#diskcache_oxide.UnsafeValueError)  
A pickle=False cache read a value that was stored with pickle.

[CorruptValueError](CorruptValueError.md#diskcache_oxide.CorruptValueError)  
A stored value couldn't be decoded: an unknown mode, or bytes that don't parse.

[ForeignValueError](ForeignValueError.md#diskcache_oxide.ForeignValueError)  
A stored value this build can't read but another might: an unknown mode, tag or extension (a newer version wrote it), or a time zone missing from this host.

[FormatMismatchError](FormatMismatchError.md#diskcache_oxide.FormatMismatchError)  
The cache directory holds a database in a format this version doesn't support.


## SQLite and System


Information about the bundled SQLite and environment.


[sqlite_version()](sqlite_version.md#diskcache_oxide.sqlite_version)  
The SQLite version reported at runtime by `sqlite3_libversion()`.

[sqlite_compile_options()](sqlite_compile_options.md#diskcache_oxide.sqlite_compile_options)  
The compile-time options of the linked SQLite (`PRAGMA compile_options`).

[BUNDLED_SQLITE_VERSION](BUNDLED_SQLITE_VERSION.md#diskcache_oxide.BUNDLED_SQLITE_VERSION)  
The SQLite version the bundled library was compiled from (the `SQLITE_VERSION` macro).
