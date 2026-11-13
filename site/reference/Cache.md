## Cache


A disk cache backed by SQLite, shared safely across threads and processes.


Usage

``` python
Cache(
    directory,
    timeout=None,
    *,
    size_limit=None,
    cull_limit=None,
    eviction_policy=None,
    serializer="msgpack",
    pickle=False,
    types=None,
    clock=None,
    max_connections=None,
    sqlite_cache_size=None,
    wal_size_limit=None,
    wal_checkpoint="manual",
    on_decode_error=None,
    durable=False
)
```


Every thread and process that opens the same directory shares one cache. The API follows `diskcache.Cache`; the deliberate differences are listed in the Divergences guide.


## Parameters


`directory: str | os.PathLike[str]`  
The cache directory. It's created (owner-only, `0700`) if it doesn't exist. If it holds a cache in another on-disk format (an older or newer release's), the cache opens in its subdirectory `diskcache-oxide-format-N` instead, N being this release's format, and logs a warning on the `diskcache_oxide` logger once per process. Once that subdirectory exists, every later open uses it. See the upgrading recipe.

`timeout: float | None = None`  
Seconds a call waits for a lock or a pending fork before raising [Timeout](Timeout.md#diskcache_oxide.Timeout) (default 60). It has to exceed work in flight plus a fork: see the Concurrency and Forks guide.

`size_limit: int | None = None`  
The disk budget in bytes, database plus value files (default 1 GiB, minimum 1 MiB). Stored in the cache, so it applies to every process.

`cull_limit: int | None = None`  
How many least-recently-stored entries a [set()](Cache.set.md#diskcache_oxide.Cache.set) evicts when the cache is over [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit) (default 10).

`eviction_policy: Literal[``"least-recently-stored", `<span class="st">`"none"``] | None`</span>` = None`  
`'least-recently-stored'` (a new cache's default) evicts the oldest stored entries when over [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit); `'none'` never evicts for size, only removing expired entries, so the cache can grow past [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit). Stored in the cache, so it applies to every process, and every write reads it afresh. None keeps the stored one. `'least-recently-used'` and `'least-frequently-used'` raise `ValueError` (D-022).

`serializer: str = ``"msgpack"`  
`'msgpack'` (the default) or `'json'`, for values that aren't plain `str` or `bytes`.

`pickle: bool = ``False`  
Allow pickle for values the serializer can't represent exactly. Without it, such values raise `TypeError` and pickled values are never read. Once the encoder has used a registration (`types`, or the global `registry`) for any part of a value, that value is never pickled: it is stored through the registration, or raises.

`types: TypeRegistry | None = None`  
A `TypeRegistry` consulted before the process-wide `diskcache_oxide.registry`, for a cache that needs its own encoding of a type. Register types at import time, in every process that opens the directory, since a row written through a registration can only be read where it is registered.

`clock: Callable[[], float] | None = None`  
A function returning the current time in seconds, used instead of the native clock. Tests pass `clock=lambda: time.time()` to follow a patched `time.time`.

`max_connections: int | None = None`  
The most SQLite connections this process opens for the directory, shared by every handle on it (default 8).

`sqlite_cache_size: int | None = None`  
SQLite's page cache per connection, in 4 KiB pages (default 256, 1 MiB).

`wal_size_limit: int | None = None`  
The write-ahead log is trimmed back to half this many bytes whenever it resets (default 64 MiB, minimum 1 MiB).

`wal_checkpoint: Literal[``"manual", `<span class="st">`"auto"``]`</span>` = ``"manual"`  
`'manual'` (the default) leaves checkpoints to [checkpoint()](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) and [close()](Cache.close.md#diskcache_oxide.Cache.close). `'auto'` also checkpoints after writes once the log is over `wal_size_limit`.

`on_decode_error: Literal[``"raise", `<span class="st">`"miss"``] | Callable[[str, Exception], object] | None`</span>` = None`  
What a read does with a stored value it can't decode. `'raise'` (the default, unless `durable=True`, where it's `'miss'`) raises. `'miss'` treats it as a miss, logs a warning on the `diskcache_oxide` logger, and deletes the row if no build could ever read it (a class removed since it was pickled, malformed bytes). A row another version or host might read ([ForeignValueError](ForeignValueError.md#diskcache_oxide.ForeignValueError)) is left in place. A callable does the same as `'miss'`, but is called with `(key, exc)` instead of the warning, after the row is dealt with; whatever it raises is logged. [UnsafeValueError](UnsafeValueError.md#diskcache_oxide.UnsafeValueError), [Timeout](Timeout.md#diskcache_oxide.Timeout), `MemoryError` and other errors that aren't about the stored bytes raise, though under `durable=True` a [Timeout](Timeout.md#diskcache_oxide.Timeout), `OSError` or `RecursionError` from a pickle hook is a miss, with the row kept, and so is a `RecursionError` from a value too deep for this thread's stack. Covers [get](Cache.get.md#diskcache_oxide.Cache.get), `cache[key]` and [pop](Cache.pop.md#diskcache_oxide.Cache.pop), which decodes before it deletes, so it keeps a row it can't read. Under `'raise'`, [pop](Cache.pop.md#diskcache_oxide.Cache.pop) still deletes a value no build could read, then raises.

`durable: bool = ``False`  
Keep the cache from taking a request down, for an app whose every caller can fall back to its source of truth (default False). An outage on a single-key call (a [Timeout](Timeout.md#diskcache_oxide.Timeout), a full disk, an I/O error, a damaged database) doesn't raise: [get](Cache.get.md#diskcache_oxide.Cache.get), [pop](Cache.pop.md#diskcache_oxide.Cache.pop) and `in` give the default or False, [set](Cache.set.md#diskcache_oxide.Cache.set), [add](Cache.add.md#diskcache_oxide.Cache.add), [touch](Cache.touch.md#diskcache_oxide.Cache.touch) and [delete](Cache.delete.md#diskcache_oxide.Cache.delete) give False, and [incr](Cache.incr.md#diskcache_oxide.Cache.incr) and [decr](Cache.decr.md#diskcache_oxide.Cache.decr) give `default + delta`. It's reported on the `diskcache_oxide` logger, at most once a minute per kind of failure. Bugs still raise (`TypeError`, `ValueError`, [UnsafeValueError](UnsafeValueError.md#diskcache_oxide.UnsafeValueError)), as do `MemoryError` and `KeyboardInterrupt`, a `RecursionError` from encoding a value too deep for this thread's stack, the constructor, admin calls (`len`, [volume](Cache.volume.md#diskcache_oxide.Cache.volume), [clear](Cache.clear.md#diskcache_oxide.Cache.clear), [expire](Cache.expire.md#diskcache_oxide.Cache.expire), [cull](Cache.cull.md#diskcache_oxide.Cache.cull), [checkpoint](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint), iteration) and anything inside a transaction held on this cache. This isn't durability in the database sense: this option can drop writes, and a dropped [set](Cache.set.md#diskcache_oxide.Cache.set), [delete](Cache.delete.md#diskcache_oxide.Cache.delete), [pop](Cache.pop.md#diskcache_oxide.Cache.pop) or [incr](Cache.incr.md#diskcache_oxide.Cache.incr) means stale data until the entry expires, or forever if it has no expiry. See "A cache that can't take a request down" in the Getting Started guide before you use it.


## Raises


`ValueError`  
A setting is out of range, such as a [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit) under 1 MiB.

`TypeError`  
An argument has the wrong type, including a `bool` for [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).

`FormatMismatchError`  
The versioned subdirectory holds a database in a format this version can't read (another format in the directory itself opens the subdirectory instead).

`LayoutMismatchError`  
The directory is a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) root or one of its shards, in any format. Also a `ValueError`, and a [FormatMismatchError](FormatMismatchError.md#diskcache_oxide.FormatMismatchError).

`Timeout`  
Another process held the database locked, or a fork stayed pending, for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).


## Examples

``` python
from diskcache_oxide import Cache

cache = Cache('/var/cache/my-app', timeout=1.0)  # every worker opens the same directory

cache.set('user:42', {'name': 'Sam', 'roles': {'admin'}}, expire=300)
cache.get('user:42')             # the dict, with roles still a set; None on a miss
cache['greeting'] = 'hello'      # never expires
if cache.add('job:nightly', 1, expire=3600):  # exactly one process wins
    run_nightly_job()
cache.close()                    # or `with Cache(...) as cache:`
```
