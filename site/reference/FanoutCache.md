## FanoutCache


A disk cache split over several SQLite databases (shards), so reads and writes on different keys


Usage

``` python
FanoutCache(
    directory,
    timeout=None,
    *,
    shards=None,
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


don't queue on one file's locks.

Each key lives in exactly one shard, chosen by a hash that is part of the on-disk format: every process, and every release, sends a key to the same shard. The root records its shard count, so every handle agrees on it. Single-key calls behave exactly as on [Cache](Cache.md#diskcache_oxide.Cache), on the key's shard. `len()` and [volume()](Cache.volume.md#diskcache_oxide.Cache.volume) add up every shard under one [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).

[clear()](Cache.clear.md#diskcache_oxide.Cache.clear), [expire()](Cache.expire.md#diskcache_oxide.Cache.expire), [cull()](Cache.cull.md#diskcache_oxide.Cache.cull) and [checkpoint()](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) run on every shard in turn, under one [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout) for the whole call. If a shard fails, the others are still done, and then the first failure raises, as its own type (so `except Timeout` still works), with two extra attributes and a note naming them:

- `failed_shards`: a tuple of the indexes of the shards that failed, in order.
- `removed` (not on [checkpoint()](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint)): how many entries the call removed in all, from every shard, including what a failed shard removed before it failed.


## Parameters


`directory: str | os.PathLike[str]`  
The root directory. It holds the manifest (`diskcache-oxide-fanout`) and one directory per shard, `000` to `NNN`. It must not be a plain [Cache](Cache.md#diskcache_oxide.Cache)'s directory.

`timeout: float | None = None`  
As for [Cache](Cache.md#diskcache_oxide.Cache) (default 60). Opening, `len()`, [volume()](Cache.volume.md#diskcache_oxide.Cache.volume), [clear()](Cache.clear.md#diskcache_oxide.Cache.clear), [expire()](Cache.expire.md#diskcache_oxide.Cache.expire), [cull()](Cache.cull.md#diskcache_oxide.Cache.cull), [checkpoint()](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) and [close()](Cache.close.md#diskcache_oxide.Cache.close) share one [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout) across every shard, not one each.

`shards: int | None = None`  
How many shards, 1 to 256. `None` (the default) uses the count the root records, or 8 for a new root. A count that differs from the recorded one raises [LayoutMismatchError](LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError) (a `ValueError`): changing it needs a new directory. Each shard takes about 4 file descriptors to open, and 2 more per connection it opens in use, so 256 shards need over 1,024: more than Linux's default `ulimit -n`. Too few raises `OSError` (`EMFILE`) with the numbers.

`size_limit: int | None = None`  
The disk budget in bytes for the whole cache, split evenly (floored) across the shards, each of which culls on its own. Each shard needs at least 1 MiB, so the total must be at least [shards](FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) MiB. With none given, a new root gets 1 GiB in all.

`cull_limit: int | None = None`  
As for [Cache](Cache.md#diskcache_oxide.Cache), per shard.

`eviction_policy: Literal[``"least-recently-stored", `<span class="st">`"none"``] | None`</span>` = None`  
As for [Cache](Cache.md#diskcache_oxide.Cache), on every shard.

`serializer: str = ``"msgpack"`  
As for [Cache](Cache.md#diskcache_oxide.Cache).

`pickle: bool = ``False`  
As for [Cache](Cache.md#diskcache_oxide.Cache).

`clock: Callable[[], float] | None = None`  
As for [Cache](Cache.md#diskcache_oxide.Cache).

`max_connections: int | None = None`  
The most SQLite connections this process opens for the whole cache, split across the shards (the first `max_connections % shards` get one more). At least [shards](FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards). The default is two per shard, and never fewer than 8 in all.

`sqlite_cache_size: int | None = None`  
As for [Cache](Cache.md#diskcache_oxide.Cache), per connection.

`wal_size_limit: int | None = None`  
As for [Cache](Cache.md#diskcache_oxide.Cache), per shard (each shard has its own write-ahead log).

`wal_checkpoint: Literal[``"manual", `<span class="st">`"auto"``]`</span>` = ``"manual"`  
As for [Cache](Cache.md#diskcache_oxide.Cache).

`on_decode_error: Literal[``"raise", `<span class="st">`"miss"``] | Callable[[str, Exception], object] | None`</span>` = None`  
As for [Cache](Cache.md#diskcache_oxide.Cache).

`durable: bool = ``False`  
As for [Cache](Cache.md#diskcache_oxide.Cache): single-key calls on a shard that fails give their miss values. One report covers every shard, named by the root directory.


## Raises


`ValueError`  
A setting is out of range, or `max_connections` is below [shards](FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards).

`LayoutMismatchError`  
[shards](FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) differs from the root's, or the directory belongs to a plain [Cache](Cache.md#diskcache_oxide.Cache) (or a shard of it does). Also a `ValueError`, and a [FormatMismatchError](FormatMismatchError.md#diskcache_oxide.FormatMismatchError).

`TypeError`  
An argument has the wrong type, including a `bool` for [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout) or [shards](FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards).

`FormatMismatchError`  
The root's manifest, or a shard's database, is in a format this version can't read.

`OSError`  
The filesystem doesn't support hard links, which creating the manifest needs, or the process can't open enough files for every shard (`ulimit -n`).

`Timeout`  
A shard stayed locked, or a fork stayed pending, for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).


## Examples

``` python
from diskcache_oxide import FanoutCache

cache = FanoutCache('/var/cache/my-app-counters', shards=8)   # shards is keyword-only
cache.incr('ip:203.0.113.7')          # as on Cache, on the key's shard
cache.shard_of('ip:203.0.113.7')      # 0 to 7, the same in every process and release
with cache.transact(key='totals') as tx:   # one shard's write lock, not all of them
    tx.set('totals', tx.get('totals', 0) + 1)
```
