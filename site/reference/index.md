# API Reference


The public API of diskcache-oxide: Cache, NullCache, CacheLike protocol, exceptions, and SQLite utilities.


## Cache


Disk-backed cache backed by SQLite, shared safely across threads and processes.


[Cache](Cache.md#diskcache_oxide.Cache)  
A disk cache backed by SQLite, shared safely across threads and processes.


## Cache Methods


Methods for the Cache class


[Cache.directory](Cache.directory.md#diskcache_oxide.Cache.directory)  

[Cache.timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout)  

[Cache.size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit)  

[Cache.cull_limit](Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit)  

[Cache.enabled](Cache.enabled.md#diskcache_oxide.Cache.enabled)  

[Cache.set()](Cache.set.md#diskcache_oxide.Cache.set)  

[Cache.add()](Cache.add.md#diskcache_oxide.Cache.add)  

[Cache.delete()](Cache.delete.md#diskcache_oxide.Cache.delete)  

[Cache.pop()](Cache.pop.md#diskcache_oxide.Cache.pop)  

[Cache.touch()](Cache.touch.md#diskcache_oxide.Cache.touch)  

[Cache.incr()](Cache.incr.md#diskcache_oxide.Cache.incr)  

[Cache.decr()](Cache.decr.md#diskcache_oxide.Cache.decr)  

[Cache.iterkeys()](Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys)  

[Cache.expire()](Cache.expire.md#diskcache_oxide.Cache.expire)  

[Cache.clear()](Cache.clear.md#diskcache_oxide.Cache.clear)  

[Cache.cull()](Cache.cull.md#diskcache_oxide.Cache.cull)  

[Cache.volume()](Cache.volume.md#diskcache_oxide.Cache.volume)  

[Cache.checkpoint()](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint)  
Checkpoint the write-ahead log and truncate it, retrying until the cache's timeout.

[Cache.close()](Cache.close.md#diskcache_oxide.Cache.close)  
Close this handle. With `checkpoint`, a WAL left larger than half of `wal_size_limit`

[Cache.__getitem__()](Cache.__getitem__.md#diskcache_oxide.Cache.__getitem__)  

[Cache.__setitem__()](Cache.__setitem__.md#diskcache_oxide.Cache.__setitem__)  

[Cache.__delitem__()](Cache.__delitem__.md#diskcache_oxide.Cache.__delitem__)  

[Cache.__contains__()](Cache.__contains__.md#diskcache_oxide.Cache.__contains__)  

[Cache.__len__()](Cache.__len__.md#diskcache_oxide.Cache.__len__)  

[Cache.__bool__()](Cache.__bool__.md#diskcache_oxide.Cache.__bool__)  

[Cache.__iter__()](Cache.__iter__.md#diskcache_oxide.Cache.__iter__)  

[Cache.__enter__()](Cache.__enter__.md#diskcache_oxide.Cache.__enter__)  

[Cache.__exit__()](Cache.__exit__.md#diskcache_oxide.Cache.__exit__)  


## NullCache


Switched-off cache behaving as if every entry expires instantly.


[NullCache](NullCache.md#diskcache_oxide.NullCache)  
A switched-off cache. See the module docstring for the rule every method follows.

[CacheLike](CacheLike.md#diskcache_oxide.CacheLike)  
What application code can rely on from a `Cache` or a `NullCache`.


## NullCache Methods


Methods for the NullCache class


[NullCache.__repr__()](NullCache.__repr__.md#diskcache_oxide.NullCache.__repr__)  

[NullCache.directory](NullCache.directory.md#diskcache_oxide.NullCache.directory)  

[NullCache.timeout](NullCache.timeout.md#diskcache_oxide.NullCache.timeout)  

[NullCache.size_limit](NullCache.size_limit.md#diskcache_oxide.NullCache.size_limit)  

[NullCache.cull_limit](NullCache.cull_limit.md#diskcache_oxide.NullCache.cull_limit)  

[NullCache.enabled](NullCache.enabled.md#diskcache_oxide.NullCache.enabled)  

[NullCache.get()](NullCache.get.md#diskcache_oxide.NullCache.get)  

[NullCache.__getitem__()](NullCache.__getitem__.md#diskcache_oxide.NullCache.__getitem__)  

[NullCache.__contains__()](NullCache.__contains__.md#diskcache_oxide.NullCache.__contains__)  

[NullCache.__len__()](NullCache.__len__.md#diskcache_oxide.NullCache.__len__)  

[NullCache.__bool__()](NullCache.__bool__.md#diskcache_oxide.NullCache.__bool__)  

[NullCache.__iter__()](NullCache.__iter__.md#diskcache_oxide.NullCache.__iter__)  

[NullCache.volume()](NullCache.volume.md#diskcache_oxide.NullCache.volume)  

[NullCache.checkpoint()](NullCache.checkpoint.md#diskcache_oxide.NullCache.checkpoint)  

[NullCache.set()](NullCache.set.md#diskcache_oxide.NullCache.set)  

[NullCache.__setitem__()](NullCache.__setitem__.md#diskcache_oxide.NullCache.__setitem__)  

[NullCache.add()](NullCache.add.md#diskcache_oxide.NullCache.add)  

[NullCache.touch()](NullCache.touch.md#diskcache_oxide.NullCache.touch)  

[NullCache.incr()](NullCache.incr.md#diskcache_oxide.NullCache.incr)  

[NullCache.decr()](NullCache.decr.md#diskcache_oxide.NullCache.decr)  

[NullCache.delete()](NullCache.delete.md#diskcache_oxide.NullCache.delete)  

[NullCache.__delitem__()](NullCache.__delitem__.md#diskcache_oxide.NullCache.__delitem__)  

[NullCache.pop()](NullCache.pop.md#diskcache_oxide.NullCache.pop)  

[NullCache.clear()](NullCache.clear.md#diskcache_oxide.NullCache.clear)  

[NullCache.iterkeys()](NullCache.iterkeys.md#diskcache_oxide.NullCache.iterkeys)  

[NullCache.expire()](NullCache.expire.md#diskcache_oxide.NullCache.expire)  

[NullCache.cull()](NullCache.cull.md#diskcache_oxide.NullCache.cull)  

[NullCache.close()](NullCache.close.md#diskcache_oxide.NullCache.close)  

[NullCache.__enter__()](NullCache.__enter__.md#diskcache_oxide.NullCache.__enter__)  

[NullCache.__exit__()](NullCache.__exit__.md#diskcache_oxide.NullCache.__exit__)  


## CacheLike Methods


Methods for the CacheLike class


[CacheLike.directory](CacheLike.directory.md#diskcache_oxide.CacheLike.directory)  

[CacheLike.timeout](CacheLike.timeout.md#diskcache_oxide.CacheLike.timeout)  

[CacheLike.size_limit](CacheLike.size_limit.md#diskcache_oxide.CacheLike.size_limit)  

[CacheLike.cull_limit](CacheLike.cull_limit.md#diskcache_oxide.CacheLike.cull_limit)  

[CacheLike.enabled](CacheLike.enabled.md#diskcache_oxide.CacheLike.enabled)  

[CacheLike.get()](CacheLike.get.md#diskcache_oxide.CacheLike.get)  

[CacheLike.set()](CacheLike.set.md#diskcache_oxide.CacheLike.set)  

[CacheLike.add()](CacheLike.add.md#diskcache_oxide.CacheLike.add)  

[CacheLike.delete()](CacheLike.delete.md#diskcache_oxide.CacheLike.delete)  

[CacheLike.pop()](CacheLike.pop.md#diskcache_oxide.CacheLike.pop)  

[CacheLike.touch()](CacheLike.touch.md#diskcache_oxide.CacheLike.touch)  

[CacheLike.incr()](CacheLike.incr.md#diskcache_oxide.CacheLike.incr)  

[CacheLike.decr()](CacheLike.decr.md#diskcache_oxide.CacheLike.decr)  

[CacheLike.iterkeys()](CacheLike.iterkeys.md#diskcache_oxide.CacheLike.iterkeys)  

[CacheLike.expire()](CacheLike.expire.md#diskcache_oxide.CacheLike.expire)  

[CacheLike.clear()](CacheLike.clear.md#diskcache_oxide.CacheLike.clear)  

[CacheLike.cull()](CacheLike.cull.md#diskcache_oxide.CacheLike.cull)  

[CacheLike.volume()](CacheLike.volume.md#diskcache_oxide.CacheLike.volume)  

[CacheLike.checkpoint()](CacheLike.checkpoint.md#diskcache_oxide.CacheLike.checkpoint)  

[CacheLike.close()](CacheLike.close.md#diskcache_oxide.CacheLike.close)  

[CacheLike.__getitem__()](CacheLike.__getitem__.md#diskcache_oxide.CacheLike.__getitem__)  

[CacheLike.__setitem__()](CacheLike.__setitem__.md#diskcache_oxide.CacheLike.__setitem__)  

[CacheLike.__delitem__()](CacheLike.__delitem__.md#diskcache_oxide.CacheLike.__delitem__)  

[CacheLike.__contains__()](CacheLike.__contains__.md#diskcache_oxide.CacheLike.__contains__)  

[CacheLike.__len__()](CacheLike.__len__.md#diskcache_oxide.CacheLike.__len__)  

[CacheLike.__bool__()](CacheLike.__bool__.md#diskcache_oxide.CacheLike.__bool__)  

[CacheLike.__iter__()](CacheLike.__iter__.md#diskcache_oxide.CacheLike.__iter__)  

[CacheLike.__enter__()](CacheLike.__enter__.md#diskcache_oxide.CacheLike.__enter__)  

[CacheLike.__exit__()](CacheLike.__exit__.md#diskcache_oxide.CacheLike.__exit__)  


## Errors


Exceptions raised by cache operations.


[Timeout](Timeout.md#diskcache_oxide.Timeout)  
The database stayed locked for longer than the cache's timeout.

[UnsafeValueError](UnsafeValueError.md#diskcache_oxide.UnsafeValueError)  
A pickle=False cache read a value that was stored with pickle.

[CorruptValueError](CorruptValueError.md#diskcache_oxide.CorruptValueError)  
A stored value couldn't be decoded: an unknown mode, or bytes that don't parse.

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
