# API Reference


The public API of diskcache-oxide: Cache, FanoutCache, NullCache, the CacheLike, TransactionalCacheLike and ShardedCacheLike protocols, the Caches registry, exceptions, types and serialization, memoize, the coordination recipes, and SQLite utilities.


## Cache


Disk-backed cache backed by SQLite, shared safely across threads and processes.


[Cache](Cache.md#diskcache_oxide.Cache)  
A disk cache backed by SQLite, shared safely across threads and processes.


## Cache Methods


Methods for the Cache class


[Cache.directory](Cache.directory.md#diskcache_oxide.Cache.directory)  
The directory the cache is in: the one given to the constructor, or its

[Cache.timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout)  
Seconds a call waits for a lock or a pending fork before raising `Timeout`.

[Cache.size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit)  
The disk budget in bytes. A `set()` that finds the cache over it evicts entries.

[Cache.cull_limit](Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit)  
The most entries a single `set()` evicts when the cache is over `size_limit`.

[Cache.eviction_policy](Cache.eviction_policy.md#diskcache_oxide.Cache.eviction_policy)  
`'least-recently-stored'` or `'none'`, as this handle last saw it stored (at open or its

[Cache.enabled](Cache.enabled.md#diskcache_oxide.Cache.enabled)  
Always True for a real cache; False for a `NullCache`.

[Cache.set()](Cache.set.md#diskcache_oxide.Cache.set)  
Store `value` under `key`, replacing any existing value.

[Cache.add()](Cache.add.md#diskcache_oxide.Cache.add)  
Store `value` only if `key` is missing or expired.

[Cache.delete()](Cache.delete.md#diskcache_oxide.Cache.delete)  
Remove `key`.

[Cache.touch()](Cache.touch.md#diskcache_oxide.Cache.touch)  
Change the expiry of an existing entry without rewriting its value.

[Cache.incr()](Cache.incr.md#diskcache_oxide.Cache.incr)  
Add `delta` to the number stored under `key`, atomically, and return the result.

[Cache.decr()](Cache.decr.md#diskcache_oxide.Cache.decr)  
Subtract `delta` from the number stored under `key`, atomically. See `incr()`.

[Cache.memoize()](Cache.memoize.md#diskcache_oxide.Cache.memoize)  
Return a decorator that caches the function's results in this cache, as diskcache's `memoize`.

[Cache.remove_old_formats()](Cache.remove_old_formats.md#diskcache_oxide.Cache.remove_old_formats)  
Remove the caches an older diskcache-oxide format left in this cache's directory: the

[Cache.check()](Cache.check.md#diskcache_oxide.Cache.check)  
Check the database and the value files against each other, as diskcache's `check()`.

[Cache.maintenance_stats()](Cache.maintenance_stats.md#diskcache_oxide.Cache.maintenance_stats)  
How background maintenance has gone on this cache in this process (see

[Cache.stats()](Cache.stats.md#diskcache_oxide.Cache.stats)  
`(hits, misses)` of `get` and `cache[key]` in this process, as diskcache's `stats()`.

[Cache.durable](Cache.durable.md#diskcache_oxide.Cache.durable)  
True for a `durable=True` handle.

[Cache.__reversed__()](Cache.__reversed__.md#diskcache_oxide.Cache.__reversed__)  
Keys in insertion order backwards, newest first (a `FanoutCache`: the last shard first).

[Cache.expire()](Cache.expire.md#diskcache_oxide.Cache.expire)  
Remove every expired entry, and its value file.

[Cache.clear()](Cache.clear.md#diskcache_oxide.Cache.clear)  
Remove every entry that existed when the call started.

[Cache.evict()](Cache.evict.md#diskcache_oxide.Cache.evict)  
Remove every entry tagged `tag` that existed when the call started.

[Cache.cull()](Cache.cull.md#diskcache_oxide.Cache.cull)  
Remove expired entries, then evict the least recently stored until under `size_limit`.

[Cache.volume()](Cache.volume.md#diskcache_oxide.Cache.volume)  
The cache's size on disk in bytes: the database plus value files.

[Cache.checkpoint()](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint)  
Copy the write-ahead log into the database and truncate it to zero.

[Cache.close()](Cache.close.md#diskcache_oxide.Cache.close)  
Close this handle and return its connections.

[Cache.pull()](Cache.pull.md#diskcache_oxide.Cache.pull)  
Remove and return the item at `side` of a queue, as diskcache's `pull`.

[Cache.peek()](Cache.peek.md#diskcache_oxide.Cache.peek)  
The item at `side` of a queue, without removing it, as diskcache's `peek`.

[Cache.peekitem()](Cache.peekitem.md#diskcache_oxide.Cache.peekitem)  
The newest entry (or with `last=False`, the oldest) of any key, without removing it, as

[Cache.transact()](Cache.transact.md#diskcache_oxide.Cache.transact)  
A write transaction, for `with cache.transact() as tx:`.

[Cache.__getitem__()](Cache.__getitem__.md#diskcache_oxide.Cache.__getitem__)  
`cache[key]`: the value, or `KeyError` if it's missing or expired, or the read failed under

[Cache.__setitem__()](Cache.__setitem__.md#diskcache_oxide.Cache.__setitem__)  
`cache[key] = value`: store a value that never expires, like `set()`. Under `durable=True` a failed write is

[Cache.__delitem__()](Cache.__delitem__.md#diskcache_oxide.Cache.__delitem__)  
`del cache[key]`: remove the entry, or raise `KeyError` if it's missing or expired, or the delete failed

[Cache.__contains__()](Cache.__contains__.md#diskcache_oxide.Cache.__contains__)  
`key in cache`: True if the key holds a live (unexpired) value. False if the check failed under

[Cache.__len__()](Cache.__len__.md#diskcache_oxide.Cache.__len__)  
The number of live entries. An entry stops counting the moment it expires, whether or not

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

[Cache.pop()](Cache.pop.md#diskcache_oxide.Cache.pop)  
Remove `key` and return its value, delivering it to exactly one caller.

[Cache.reset()](Cache.reset.md#diskcache_oxide.Cache.reset)  
Read `size_limit`, `cull_limit` or `eviction_policy` as stored now, or persist a new value and

[Cache.iterkeys()](Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys)  
Iterate over keys in sorted order, optionally only those starting with `prefix`.

[Cache.push()](Cache.push.md#diskcache_oxide.Cache.push)  
Push `value` onto a queue and return its key, as diskcache's `push`.


## FanoutCache


A cache sharded over several SQLite databases, so writers in many processes stop queueing on one write lock.


[FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache)  
A disk cache split over several SQLite databases (shards), so reads and writes on different keys


## FanoutCache Methods


Methods for the FanoutCache class


[FanoutCache.directory](FanoutCache.directory.md#diskcache_oxide.FanoutCache.directory)  
The root directory: the one given to the constructor, or its

[FanoutCache.timeout](FanoutCache.timeout.md#diskcache_oxide.FanoutCache.timeout)  
Seconds a call waits for a lock or a pending fork before raising `Timeout`.

[FanoutCache.shards](FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards)  
How many shards the root has.

[FanoutCache.size_limit](FanoutCache.size_limit.md#diskcache_oxide.FanoutCache.size_limit)  
The disk budget in bytes: the sum of every shard's.

[FanoutCache.cull_limit](FanoutCache.cull_limit.md#diskcache_oxide.FanoutCache.cull_limit)  
The most entries a single `set()` evicts from its shard when that shard is over its share.

[FanoutCache.eviction_policy](FanoutCache.eviction_policy.md#diskcache_oxide.FanoutCache.eviction_policy)  
Shard 0's eviction policy, as for `cull_limit`.

[FanoutCache.enabled](FanoutCache.enabled.md#diskcache_oxide.FanoutCache.enabled)  
Always True for a real cache; False for a `NullCache`.

[FanoutCache.shard_of()](FanoutCache.shard_of.md#diskcache_oxide.FanoutCache.shard_of)  
The shard `key` lives in, from 0 to `shards - 1`: the same in every process and release.

[FanoutCache.transact()](FanoutCache.transact.md#diskcache_oxide.FanoutCache.transact)  
A write transaction on the shard `key` lives in, for `with cache.transact(key='total') as tx:`.

[FanoutCache.set()](FanoutCache.set.md#diskcache_oxide.FanoutCache.set)  
As `Cache.set`, on the key's shard.

[FanoutCache.add()](FanoutCache.add.md#diskcache_oxide.FanoutCache.add)  
As `Cache.add`, on the key's shard: atomic across threads and processes.

[FanoutCache.delete()](FanoutCache.delete.md#diskcache_oxide.FanoutCache.delete)  
As `Cache.delete`, on the key's shard.

[FanoutCache.touch()](FanoutCache.touch.md#diskcache_oxide.FanoutCache.touch)  
As `Cache.touch`, on the key's shard.

[FanoutCache.incr()](FanoutCache.incr.md#diskcache_oxide.FanoutCache.incr)  
As `Cache.incr`, on the key's shard: atomic across threads and processes.

[FanoutCache.decr()](FanoutCache.decr.md#diskcache_oxide.FanoutCache.decr)  
As `Cache.decr`, on the key's shard.

[FanoutCache.volume()](FanoutCache.volume.md#diskcache_oxide.FanoutCache.volume)  
The total disk use in bytes of every shard (databases plus value files).

[FanoutCache.expire()](FanoutCache.expire.md#diskcache_oxide.FanoutCache.expire)  
Remove every expired entry from every shard, and its value file, as `Cache.expire` does.

[FanoutCache.clear()](FanoutCache.clear.md#diskcache_oxide.FanoutCache.clear)  
Remove every entry that existed when the call started, from every shard.

[FanoutCache.evict()](FanoutCache.evict.md#diskcache_oxide.FanoutCache.evict)  
Remove every entry tagged `tag` that existed when the call started, from every shard, as

[FanoutCache.cull()](FanoutCache.cull.md#diskcache_oxide.FanoutCache.cull)  
Remove expired entries, then evict the least recently stored from each shard until it's

[FanoutCache.checkpoint()](FanoutCache.checkpoint.md#diskcache_oxide.FanoutCache.checkpoint)  
Checkpoint every shard's write-ahead log and truncate it to zero, as `Cache.checkpoint` does.

[FanoutCache.memoize()](FanoutCache.memoize.md#diskcache_oxide.FanoutCache.memoize)  
Return a decorator that caches the function's results in this cache, as diskcache's `memoize`.

[FanoutCache.remove_old_formats()](FanoutCache.remove_old_formats.md#diskcache_oxide.FanoutCache.remove_old_formats)  
As `Cache.remove_old_formats()`, for this cache's root directory: an older format's

[FanoutCache.check()](FanoutCache.check.md#diskcache_oxide.FanoutCache.check)  
Check the database and the value files against each other, as diskcache's `check()`.

[FanoutCache.maintenance_stats()](FanoutCache.maintenance_stats.md#diskcache_oxide.FanoutCache.maintenance_stats)  
How background maintenance has gone on this cache in this process (see

[FanoutCache.stats()](FanoutCache.stats.md#diskcache_oxide.FanoutCache.stats)  
`(hits, misses)` of `get` and `cache[key]` in this process, as diskcache's `stats()`.

[FanoutCache.durable](FanoutCache.durable.md#diskcache_oxide.FanoutCache.durable)  
True for a `durable=True` handle.

[FanoutCache.__reversed__()](FanoutCache.__reversed__.md#diskcache_oxide.FanoutCache.__reversed__)  
Keys in insertion order backwards, newest first (a `FanoutCache`: the last shard first).

[FanoutCache.__iter__()](FanoutCache.__iter__.md#diskcache_oxide.FanoutCache.__iter__)  
Iterate over every key once: shard 0's in insertion order, then shard 1's, and so on.

[FanoutCache.close()](FanoutCache.close.md#diskcache_oxide.FanoutCache.close)  
Close this handle on every shard, as `Cache.close()` does for one.

[FanoutCache.__getitem__()](FanoutCache.__getitem__.md#diskcache_oxide.FanoutCache.__getitem__)  
`cache[key]`, as on `Cache`.

[FanoutCache.__setitem__()](FanoutCache.__setitem__.md#diskcache_oxide.FanoutCache.__setitem__)  
`cache[key] = value`, as on `Cache`.

[FanoutCache.__delitem__()](FanoutCache.__delitem__.md#diskcache_oxide.FanoutCache.__delitem__)  
`del cache[key]`, as on `Cache`.

[FanoutCache.__contains__()](FanoutCache.__contains__.md#diskcache_oxide.FanoutCache.__contains__)  
`key in cache`, as on `Cache`.

[FanoutCache.__len__()](FanoutCache.__len__.md#diskcache_oxide.FanoutCache.__len__)  
The number of live entries in every shard: expired ones don't count, as on `Cache`.

[FanoutCache.__bool__()](FanoutCache.__bool__.md#diskcache_oxide.FanoutCache.__bool__)  
Always True, even when empty, so `if cache:` means "caching is on".

[FanoutCache.__enter__()](FanoutCache.__enter__.md#diskcache_oxide.FanoutCache.__enter__)  
Return the cache, for `with FanoutCache(...) as cache:`.

[FanoutCache.__exit__()](FanoutCache.__exit__.md#diskcache_oxide.FanoutCache.__exit__)  
Close the cache, as `close()` does.

[FanoutCache.get()](FanoutCache.get.md#diskcache_oxide.FanoutCache.get)  
As `Cache.get`, on the key's shard.

[FanoutCache.pop()](FanoutCache.pop.md#diskcache_oxide.FanoutCache.pop)  
As `Cache.pop`, on the key's shard.

[FanoutCache.reset()](FanoutCache.reset.md#diskcache_oxide.FanoutCache.reset)  
Read `size_limit`, `cull_limit` or `eviction_policy` as stored now, or persist a new value and

[FanoutCache.iterkeys()](FanoutCache.iterkeys.md#diskcache_oxide.FanoutCache.iterkeys)  
Iterate over keys in sorted order, optionally only those starting with `prefix`.


## NullCache


Switched-off cache behaving as if every entry expires instantly.


[NullCache](NullCache.md#diskcache_oxide.NullCache)  
A switched-off cache, so app code needs no `if cache is None` checks.

[CacheLike](CacheLike.md#diskcache_oxide.CacheLike)  
What application code can rely on from a `Cache`, a `FanoutCache` or a `NullCache`.

[TransactionalCacheLike](TransactionalCacheLike.md#diskcache_oxide.TransactionalCacheLike)  
A `CacheLike` with `transact()` and the queue calls (`push`, `pull`, `peek`, `peekitem`), which

[ShardedCacheLike](ShardedCacheLike.md#diskcache_oxide.ShardedCacheLike)  
A `CacheLike` with a sharded cache's `transact(*, key, retry=False)`, which `FanoutCache` and


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

[NullCache.eviction_policy](NullCache.eviction_policy.md#diskcache_oxide.NullCache.eviction_policy)  
The eviction policy passed to the constructor (default 'least-recently-stored'). Unused.

[NullCache.enabled](NullCache.enabled.md#diskcache_oxide.NullCache.enabled)  
Always False: caching is off.

[NullCache.durable](NullCache.durable.md#diskcache_oxide.NullCache.durable)  
Always False: a `NullCache` has no outages to answer.

[NullCache.check()](NullCache.check.md#diskcache_oxide.NullCache.check)  
Always `[]`: there is nothing to check.

[NullCache.remove_old_formats()](NullCache.remove_old_formats.md#diskcache_oxide.NullCache.remove_old_formats)  
Always `[]`: a `NullCache` has no directory to clean up.

[NullCache.maintenance_stats()](NullCache.maintenance_stats.md#diskcache_oxide.NullCache.maintenance_stats)  
All zero: a `NullCache` has nothing to maintain.

[NullCache.stats()](NullCache.stats.md#diskcache_oxide.NullCache.stats)  
Always `(0, 0)`: nothing is looked up.

[NullCache.reset()](NullCache.reset.md#diskcache_oxide.NullCache.reset)  
Read `size_limit`, `cull_limit` or `eviction_policy`, or set it (checked as `Cache` checks it)

[NullCache.get()](NullCache.get.md#diskcache_oxide.NullCache.get)  
Return `default`, as every entry has already expired. With `expire_time=True` or `tag=True`,

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

[NullCache.__reversed__()](NullCache.__reversed__.md#diskcache_oxide.NullCache.__reversed__)  
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
Return `default`, shaped as `get()` returns it. With `invalidate=`, also delete the key from

[NullCache.push()](NullCache.push.md#diskcache_oxide.NullCache.push)  
Store nothing and return the key an empty queue's first item gets: the item expires the instant

[NullCache.pull()](NullCache.pull.md#diskcache_oxide.NullCache.pull)  
Return `default` (widened as `Cache.pull` widens it): the queue is always empty. Never forwarded

[NullCache.peek()](NullCache.peek.md#diskcache_oxide.NullCache.peek)  
Return `default`, as `pull` does.

[NullCache.peekitem()](NullCache.peekitem.md#diskcache_oxide.NullCache.peekitem)  
Raise `KeyError`: the cache is always empty.

[NullCache.clear()](NullCache.clear.md#diskcache_oxide.NullCache.clear)  
0, or with `invalidate=`, clear that cache (passing `timeout` on) and return its count.

[NullCache.evict()](NullCache.evict.md#diskcache_oxide.NullCache.evict)  
0, or with `invalidate=`, evict `tag` from that cache (passing `timeout` on) and return its

[NullCache.memoize()](NullCache.memoize.md#diskcache_oxide.NullCache.memoize)  
As `Cache.memoize`, but every call runs the function, as every entry has already expired. The

[NullCache.iterkeys()](NullCache.iterkeys.md#diskcache_oxide.NullCache.iterkeys)  
Yield nothing, or with `invalidate=`, that cache's keys, so list-then-delete still works.

[NullCache.transact()](NullCache.transact.md#diskcache_oxide.NullCache.transact)  
A no-op context manager that yields this `NullCache`.

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

[CacheLike.eviction_policy](CacheLike.eviction_policy.md#diskcache_oxide.CacheLike.eviction_policy)  
`'least-recently-stored'` or `'none'`.

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

[CacheLike.durable](CacheLike.durable.md#diskcache_oxide.CacheLike.durable)  
True for a `durable=True` handle (always False on a `NullCache`).

[CacheLike.check()](CacheLike.check.md#diskcache_oxide.CacheLike.check)  
Check the database and value files against each other; `[]` on a `NullCache`.

[CacheLike.remove_old_formats()](CacheLike.remove_old_formats.md#diskcache_oxide.CacheLike.remove_old_formats)  
Remove caches an older format left in this cache's directory; `[]` on a `NullCache`.

[CacheLike.maintenance_stats()](CacheLike.maintenance_stats.md#diskcache_oxide.CacheLike.maintenance_stats)  
How background maintenance has gone on this cache in this process; all zero on a `NullCache`.

[CacheLike.stats()](CacheLike.stats.md#diskcache_oxide.CacheLike.stats)  
`(hits, misses)` of `get` in this process; `(0, 0)` on a `NullCache`.

[CacheLike.reset()](CacheLike.reset.md#diskcache_oxide.CacheLike.reset)  
Read or set `size_limit`, `cull_limit` or `eviction_policy`.

[CacheLike.__reversed__()](CacheLike.__reversed__.md#diskcache_oxide.CacheLike.__reversed__)  
Keys in insertion order backwards, newest first.

[CacheLike.memoize()](CacheLike.memoize.md#diskcache_oxide.CacheLike.memoize)  
A decorator caching a function's results in this cache (diskcache's `memoize`).

[CacheLike.iterkeys()](CacheLike.iterkeys.md#diskcache_oxide.CacheLike.iterkeys)  
Iterate over keys in sorted order, optionally only those starting with `prefix`.

[CacheLike.expire()](CacheLike.expire.md#diskcache_oxide.CacheLike.expire)  
Remove every expired entry. Returns how many were removed.

[CacheLike.clear()](CacheLike.clear.md#diskcache_oxide.CacheLike.clear)  
Remove every entry that existed when the call started. Returns how many.

[CacheLike.evict()](CacheLike.evict.md#diskcache_oxide.CacheLike.evict)  
Remove every entry tagged `tag` that existed when the call started. Returns how many.

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
The number of live entries: expired ones don't count.

[CacheLike.__bool__()](CacheLike.__bool__.md#diskcache_oxide.CacheLike.__bool__)  
True if caching is on, so `if cache:` reads naturally.

[CacheLike.__iter__()](CacheLike.__iter__.md#diskcache_oxide.CacheLike.__iter__)  
Iterate over every key once. A `Cache` yields them in insertion order.

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
Shows the base directory and the registered names.

[Caches.register()](Caches.register.md#diskcache_oxide.Caches.register)  
Register a cache. Nothing opens until its first lookup.

[Caches.__contains__()](Caches.__contains__.md#diskcache_oxide.Caches.__contains__)  
Whether `name` is registered.

[Caches.__iter__()](Caches.__iter__.md#diskcache_oxide.Caches.__iter__)  
The registered names, in registration order.

[Caches.__len__()](Caches.__len__.md#diskcache_oxide.Caches.__len__)  
How many caches are registered.

[Caches.directory()](Caches.directory.md#diskcache_oxide.Caches.directory)  
The directory the named cache is registered at. After a format change the cache itself

[Caches.__getitem__()](Caches.__getitem__.md#diskcache_oxide.Caches.__getitem__)  
The named plain cache: opened on first use, then the same object every time.

[Caches.fanout()](Caches.fanout.md#diskcache_oxide.Caches.fanout)  
The named sharded cache (one registered with `shards=`), looked up as `caches[name]` is.

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

[Caches.remove_old_formats()](Caches.remove_old_formats.md#diskcache_oxide.Caches.remove_old_formats)  
Run `remove_old_formats()` on every registered cache, opening each one, even while it's

[Caches.close_all()](Caches.close_all.md#diskcache_oxide.Caches.close_all)  
Close every opened cache and forget it; a later lookup opens it again.

[Caches.__enter__()](Caches.__enter__.md#diskcache_oxide.Caches.__enter__)  
Return the registry, for `with Caches(...) as caches:`.

[Caches.__exit__()](Caches.__exit__.md#diskcache_oxide.Caches.__exit__)  
Close every opened cache, as `close_all()` does.


## Errors


Exceptions raised by cache operations.


[Timeout](Timeout.md#diskcache_oxide.Timeout)  
A lock stayed held, or a fork stayed pending, for longer than the cache's timeout.

[TransactionBusy](TransactionBusy.md#diskcache_oxide.TransactionBusy)  
A thread holding one cache's transaction couldn't begin another's, because another thread holds its write

[UnsafeValueError](UnsafeValueError.md#diskcache_oxide.UnsafeValueError)  
A pickle=False cache read a value that was stored with pickle.

[CorruptValueError](CorruptValueError.md#diskcache_oxide.CorruptValueError)  
A stored value couldn't be decoded: an unknown mode, or bytes that don't parse.

[ForeignValueError](ForeignValueError.md#diskcache_oxide.ForeignValueError)  
A stored value this build can't read but another might: an unknown mode, tag or extension (a newer version wrote

[FormatMismatchError](FormatMismatchError.md#diskcache_oxide.FormatMismatchError)  
A database or manifest in a format this version doesn't support, where opening the cache's

[LayoutMismatchError](LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError)  
The directory's layout doesn't match how it was opened: a plain `Cache` on a `FanoutCache` root or shard, a

[TransactionAborted](TransactionAborted.md#diskcache_oxide.TransactionAborted)  
A held transaction didn't commit and can't: it was rolled back (after an error inside it, or by another thread

[CorruptKeyError](CorruptKeyError.md#diskcache_oxide.CorruptKeyError)  
A stored key couldn't be read: its bytes aren't a key this build wrote (a damaged page or file). Raised by

[UnknownFileWarning](UnknownFileWarning.md#diskcache_oxide.UnknownFileWarning)  
`check()` found a file in a cache directory that no entry names (removed with `fix=True` when

[EmptyDirWarning](EmptyDirWarning.md#diskcache_oxide.EmptyDirWarning)  
diskcache's warning for an empty folder. Kept for code that imports it: `check()` never issues


## Types and serialization


Storing your own types without pickle, and the key type.


[register_type()](register_type.md#diskcache_oxide.register_type)  
Register `cls` in the process-wide `registry`, so every cache can store it without pickle.

[unregister_type()](unregister_type.md#diskcache_oxide.unregister_type)  
Remove `cls` from the process-wide `registry`. Returns whether it was registered.

[registered_types()](registered_types.md#diskcache_oxide.registered_types)  
The process-wide registrations, as `{name: cls}`.

[registry](registry.md#diskcache_oxide.registry)  
The process-wide `TypeRegistry`, consulted by every cache after the one passed as `types=`.

[TypeRegistry](TypeRegistry.md#diskcache_oxide.TypeRegistry)  
Types the codecs can store without pickle: per-type `encode` and `decode` functions, or a dataclass's

[Key](Key.md#diskcache_oxide.Key)  
A cache key: `str`, `bytes`, `int` (64-bit), `float` (not NaN), `bool`, `None`, or a tuple of those,


## Memoize


The typed wrapper memoize() returns, and its sentinels.


[Memoized](Memoized.md#diskcache_oxide.Memoized)  
A function wrapped by `memoize`: called as the function was, plus `__cache_key__`.

[Memoizer](Memoizer.md#diskcache_oxide.Memoizer)  
What `cache.memoize(...)` returns: a decorator.

[ENOVAL](ENOVAL.md#diskcache_oxide.ENOVAL)  
diskcache's "no value" sentinel. Pass it as `default=` to tell a miss from a stored `None`

[UNKNOWN](UNKNOWN.md#diskcache_oxide.UNKNOWN)  
diskcache's "unknown value" sentinel, kept for code written against diskcache. A singleton that


## Coordination


Locks, semaphores, throttles and other recipes that hold across every process on the machine.


[Lock](Lock.md#diskcache_oxide.Lock)  
A lock shared by every thread and process on the cache: `acquire()`, `release()`, `locked()`,

[RLock](RLock.md#diskcache_oxide.RLock)  
A reentrant lock shared by every thread and process on the cache: the thread holding it may

[BoundedSemaphore](BoundedSemaphore.md#diskcache_oxide.BoundedSemaphore)  
A semaphore shared by every thread and process on the cache: at most `value` holders at once.

[throttle()](throttle.md#diskcache_oxide.throttle)  
A decorator allowing at most `count` calls every `seconds`, across every thread and process on

[barrier()](barrier.md#diskcache_oxide.barrier)  
A decorator running the function under a lock made by `lock_factory(cache, key, expire=...)`

[Averager](Averager.md#diskcache_oxide.Averager)  
A running average stored under `key`: `add(value)`, `get()`, `pop()`.

[memoize_stampede()](memoize_stampede.md#diskcache_oxide.memoize_stampede)  
`memoize` with cache stampede protection, as diskcache's: a result close to expiry is


## SQLite and System


Information about the bundled SQLite and environment.


[sqlite_version()](sqlite_version.md#diskcache_oxide.sqlite_version)  
The SQLite version reported at runtime by `sqlite3_libversion()`.

[sqlite_compile_options()](sqlite_compile_options.md#diskcache_oxide.sqlite_compile_options)  
The compile-time options of the linked SQLite (`PRAGMA compile_options`).

[BUNDLED_SQLITE_VERSION](BUNDLED_SQLITE_VERSION.md#diskcache_oxide.BUNDLED_SQLITE_VERSION)  
The SQLite version the bundled library was compiled from (the `SQLITE_VERSION` macro).
