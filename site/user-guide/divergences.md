# Divergences from python-diskcache

diskcache-oxide's test suite is a conformance suite: the same black-box tests run against real diskcache 5.6.3 and against diskcache-oxide. Where they behave differently, it's on purpose, and it's written down here. Running one suite against two implementations turned out to be a bug detector pointed in both directions: most of the differences below are diskcache bugs the suite found, each with a reproduction in the repository's `upstream/` folder.

A divergence is never a way to skip a failing test. The full record, including test names and measurements, is [tests/DIVERGENCES.md](https://github.com/mikeckennedy/diskcache-oxide/blob/main/tests/DIVERGENCES.md).


# Summary

| Ref | Topic | diskcache 5.6.3 | diskcache-oxide |
|----|----|----|----|
| D-001 | Values | Pickles everything | msgpack or JSON with type tags; pickle opt-in; never unpickles unless opted in |
| D-002 | Database file | `cache.db` | `diskcache-oxide.sqlite` |
| D-003 | Clock | `time.time()` | Native; `clock=` to control it |
| D-004 | Keys | Anything picklable | `str` only, for now |
| D-005 | Minimum [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) | None; tiny limits silently empty the cache | 1 MiB, else `ValueError` |
| D-006 | Empty-cache [volume()](../reference/Cache.volume.md#diskcache_oxide.Cache.volume) | Its own value | A different value |
| D-007 | [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) | Subclasses `Exception` | Subclasses `TimeoutError` |
| D-008 | Top-level NaN | Reads back as `None` | Round-trips |
| D-009 | Truthiness | Empty cache is falsy | [Cache](../reference/Cache.md#diskcache_oxide.Cache) always truthy, [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) falsy |
| D-010 | [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) | Can stop after 100 rows | Removes every expired entry |
| D-011 | [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) | Also deletes writes made while it runs | Removes only what existed when it started |
| D-012 | Fork during another thread's write | Child's writes stall for the timeout | Child can always write |
| D-013 | Missing value file | Miss, but the key still reads as present | Miss, and the row is removed |
| D-014 | `expire=True`, `timeout=True` | Treated as `1` | `TypeError` |
| D-015 | New directory permissions | Follow the umask | `0700` |
| D-016 | Reading a large value during an overwrite | Occasional spurious miss | Always the old or new value |
| D-017 | Connections and fds | One connection per thread | A bounded pool |
| D-018 | [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) | Closes only the calling thread's connection | Closes every connection |
| D-019 | Opening an existing cache | Writes to the database | Never writes unless settings change |
| D-020 | Forking inside your own transaction | Child stalls for the timeout | Fails fast with `RuntimeError` |
| D-021 | [directory](../reference/Cache.directory.md#diskcache_oxide.Cache.directory) | Optional (temp dir) | Required |
| D-022 | `disk=`, `eviction_policy=` | Supported | Not supported (policies coming) |
| D-023 | SQLite memory | 32 MiB cache + 64 MiB mmap per thread | 1 MiB per pooled connection |
| D-024 | WAL file size | Can grow past 1 GiB and stay there | Trimmed; [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) on demand |
| D-025 | A key iterator used by two threads at once | `ValueError` from the generator | `ValueError`, from a native iterator with no `send()` or [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) |
| D-026 | [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) of a value it can't read | Row is deleted, then the decode fails | Decodes first, so the row stays unless no build could read it |
| D-027 | A call behind a pending fork | Never waits: there's no fork barrier | Waits within its [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout), then raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) |
| D-028 | An outage on a single-key call | [Cache](../reference/Cache.md#diskcache_oxide.Cache) raises; [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) answers [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) alone as a miss | Raises by default; `durable=True` answers it as a miss or `False`, and reports it |
| D-029 | `with cache.transact() as tx:` | `tx` is `None`; a second handle's [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) inside waits out the timeout | `tx` is the cache; every handle on the directory joins the outer transaction |
| D-030 | A transaction on a second, busy cache | Waits for the timeout; `retry=True` loops on any `OperationalError` | [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy) at once; `retry=True` retries only [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) |
| D-031 | Ending a transaction on another thread | "May not be shared between threads" | Allowed, within its [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout); the owner's exit raises unless it committed |
| D-032 | [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) shard count | Not recorded; a different `shards=` quietly misroutes keys | Recorded in a manifest; a different `shards=` raises [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError) |
| D-033 | Which shard a key lives in | adler32; no [iterkeys()](../reference/Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys) | XXH3-64, frozen in the format; `shard_of(key)`; [iterkeys()](../reference/Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys) merges the shards in sorted order |
| D-034 | A busy shard on a single-key call | Swallows [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) (10 ms default); [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) swallows any `OperationalError` | Raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout); `durable=True` answers it as a miss and reports it |
| D-035 | [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire), [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) over the shards | Retries each shard forever | One [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) for the call; raises with `failed_shards` and `removed` |
| D-036 | [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) | Divided as a float, no minimum; reads shard 0's share | Split as integers, 1 MiB per shard minimum; reads back as the total |
| D-037 | `max_connections` | One connection per thread per shard (264 fds at 64 threads) | A total across the shards; fds within `2 × max_connections + 2 × shards` |
| D-038 | [Cache](../reference/Cache.md#diskcache_oxide.Cache) and [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) on each other's directory | Quietly creates or reuses a database | [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError), creating nothing |
| D-039 | [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) constructor and features | `FanoutCache(path, 4)`; sub-collections, tags, [stats()](../reference/Caches.stats.md#diskcache_oxide.Caches.stats), `check()` and more | [shards](../reference/FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) is keyword-only; those features aren't there yet |
| D-040 | [FanoutCache.transact()](../reference/FanoutCache.transact.md#diskcache_oxide.FanoutCache.transact) | Locks every shard; `tx` is `None` | `transact(*, key)` locks one shard and refuses other shards' keys |
| D-041 | Your own types | A `Disk` subclass, or pickle | `register_type()` per type; stored tagged, read by an allowlist; never pickled |
| D-042 | Large values' folders | Two levels (65,536 folders); a folder goes with its last file | One level (at most 256); empty ones go at the last close and in [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) |
| D-043 | A large write after the cache's folder was deleted | Re-creates the folders and succeeds | `OSError`; nothing is re-created |


# Safety


## D-001: no pickle by default

diskcache pickles every value. diskcache-oxide stores `str` and `bytes` raw and everything else as msgpack or tagged JSON. A value the format can't represent exactly raises `TypeError` at [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) unless the cache was opened with `pickle=True`. A cache opened without it never unpickles: reading a pickled value written by a `pickle=True` peer raises [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) and leaves the row alone. Unpickling runs code from whatever is in the cache directory, and silently pickling hides values that were never meant to be cached. See [Serialization](serialization.md).


## D-015: owner-only directories

A cache directory diskcache-oxide creates, and its value-file subdirectories, are `0700`. Existing directories are never changed. Anyone who can write to a cache directory can poison the cache, and its contents (keys with IP addresses in them, for example) can be private.


## D-021: [directory](../reference/Cache.directory.md#diskcache_oxide.Cache.directory) is required

diskcache creates a temporary directory if you don't give one. A cache directory is shared state and a security boundary, and an implicit temp directory is almost never what a multi-process app wants.


# Correctness


## D-008: NaN round-trips

In diskcache, `set('k', float('nan'))` succeeds but `get('k')` returns `None`, ignoring any default. In diskcache-oxide it's a float like any other.


## D-010: [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) removes everything that has expired

diskcache's [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) works in chunks of 100, and when expired entries share one expiry time it stops after the first chunk. With 250 such entries, one call removes 100. diskcache-oxide removes all of them, still in chunks so writers can get in between.


## D-011: [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) keeps concurrent writes

diskcache's [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) keeps deleting until the cache is empty, including entries written while it runs. In a reproduction with 50,000 entries and a concurrent writer, it deleted 76,000 writes that had already succeeded, and its running time grew with the write rate. diskcache-oxide's [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) removes only entries that existed when it started, and always finishes.


## D-013 and D-016: large values under pressure

If a large value's file disappears, diskcache returns the default but the key still reads as present in `in` and `len`. diskcache-oxide treats it as a miss and removes the orphaned row.

When one process overwrites a large value while others read it, diskcache occasionally returns the default for a key that was never deleted, which makes callers recompute a hot value for nothing. diskcache-oxide's readers detect the overwrite and retry, so they always get a whole value, old or new.


## D-014: no booleans for numbers

`bool` is an `int` subclass, so in diskcache `expire=True` is a one-second TTL and `expire=False` stores an already-expired entry. diskcache-oxide raises `TypeError`: a bool there is almost always an argument in the wrong position.


# Processes and forks


## D-012: forks while another thread writes

When a process forks while another of its threads is in a write transaction, diskcache's child inherits a lock nobody in the child will release, and its first write stalls for the full timeout (60 seconds by default). Under a busy read/write load that hits 20-50% of forks. diskcache-oxide's fork barrier waits for in-flight transactions before the fork, so the child can always write. See [Concurrency and forks](concurrency-and-forks.md).


## D-020: forking inside your own transaction

CPython won't let a library stop a fork, and a transaction held by the forking thread itself can't be waited for. In diskcache the child stalls behind it. In diskcache-oxide, the child's copy of the transaction is poisoned and its writes to that database raise `RuntimeError` immediately, and the parent gets a `RuntimeWarning`.


# Transactions


## D-029: `tx` is the cache, and every handle shares it

diskcache's `with cache.transact() as tx:` binds `tx` to `None`, and a second [Cache](../reference/Cache.md#diskcache_oxide.Cache) on the same directory has its own connection, so its [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) inside the first one's block waits for the write lock until it times out. In diskcache-oxide `tx` is the cache itself, and on the thread that entered the block every call on that directory runs in the transaction, through any handle: a [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) on a second handle joins the outer one instead of waiting. Other threads' calls never join it. See [Transactions](transactions.md).


## D-030: [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy), and what `retry=True` retries

Inside one cache's transaction, diskcache's [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) on a second cache waits for its write lock until it times out, and `retry=True` loops on any `sqlite3.OperationalError`. When another thread holds that lock, diskcache-oxide raises [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy), a [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) subclass, at once: that thread could be waiting for a lock this one holds, and no timeout setting fixes that. `retry=True` retries a plain [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) and never [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy), and checks for `Ctrl-C` between attempts.


## D-031: a transaction can end on another thread

diskcache says a transaction "may not be shared between threads". In diskcache-oxide any thread may commit or roll back the owner's transaction through its [__exit__](../reference/Cache.__exit__.md#diskcache_oxide.Cache.__exit__), within the transaction's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout). A commit that can't run in time raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) and rolls back, and the owner's own exit afterwards raises `RuntimeError` unless the transaction committed, so a `with` block never exits cleanly when its writes are gone. An owner's exit during another thread's commit waits for the outcome.


# Resources


## D-017 and D-018: bounded connections

diskcache opens a connection for every thread that uses a cache, so 64 threads hold 131 files open in the cache directory, and [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) closes only the calling thread's connection. diskcache-oxide uses a pool per database file, at most `max_connections` (default 8), and the last [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) closes every connection, so the directory is safe to move or delete afterwards.


## D-019: opening never writes

Opening an existing diskcache commits a write transaction even when nothing changed. diskcache-oxide's open, and reads after it, never write, so a read-mostly worker or a read-only copy of the directory never takes the write lock.


## D-023: bounded SQLite memory

diskcache's default is a 32 MiB page cache and a 64 MiB memory map per connection, with one connection per thread. diskcache-oxide's `sqlite_cache_size` defaults to 256 pages (1 MiB) per pooled connection, so SQLite's memory per directory stays around 8 MiB per process.


## D-024: a WAL that shrinks

With readers in other processes, a burst of writes makes SQLite's write-ahead log grow to fit the burst and stay that size: hundreds of MiB to over 1 GiB beside a database of a few MiB. diskcache-oxide trims the log whenever it resets (`wal_size_limit`), offers [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) to empty it on demand, checkpoints a large log at [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close), and can checkpoint automatically with `wal_checkpoint='auto'`, which tries a few times in a row so that a reader in flight in another process doesn't defeat it. See [Storage and WAL](storage-and-wal.md).


# API differences

- **D-002:** the database file is `diskcache-oxide.sqlite`, so the two libraries never misread each other's files.
- **D-003:** the clock is read natively; pass `clock=` to control it in tests.
- **D-004:** keys are `str` only until Phase 6a. They're stored with a type tag, so other key types won't need a format change.
- **D-005:** [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) must be at least 1 MiB. In diskcache, a limit below the empty database's size silently evicts every value, including the one just stored.
- **D-006:** an empty cache's [volume()](../reference/Cache.volume.md#diskcache_oxide.Cache.volume) differs, so a [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) leaves slightly different room.
- **D-007:** [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) subclasses `TimeoutError`, so `except TimeoutError` catches it too.
- **D-009:** `bool(Cache)` is always True and `bool(NullCache)` is False, so `if cache:` means "caching is on".
- **D-022:** there's no `disk=` (a Python `Disk` class would put Python code on every read and write) and no `eviction_policy=` until Phase 6a adds the other policies.
- **D-041:** a type the codec doesn't know is registered per type with `register_type(cls, name=..., encode=..., decode=...)` (a dataclass needs only a name), process-wide or per cache with `types=`. It's stored as a tagged value and read back only where the name is registered, so nothing is imported by name; a registered type is never pickled; a `decode` error about the row is a [CorruptValueError](../reference/CorruptValueError.md#diskcache_oxide.CorruptValueError) with yours as `__cause__`. See [Your own types](serialization.md#your-own-types).
- **D-028:** `Cache(..., durable=True)` has no counterpart in diskcache. It answers an outage on a single-key call as a miss or `False`, and it can drop writes. Beginning a transaction, and anything inside one, still raises. See [A cache that can't take a request down](getting-started.md#a-cache-that-cant-take-a-request-down).
- **D-042:** a large value's file sits one folder deep, in at most 256 folders. A delete removes only the file; the last close in a process removes folders left empty and untouched for 30 seconds, and [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) removes the ones it emptied. A [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache)'s shards don't sweep yet. Before this, empty folders were never removed and a long-lived cache could hold 65,536 of them, about 257 MiB on ext4. See [Storage and WAL](storage-and-wal.md).
- **D-043:** if a cache's folder is deleted while a process has it open, that process's next large write raises `OSError` instead of re-creating the folder. Delete a cache's folder only with every process stopped.
- **D-025:** `iter(cache)` and [iterkeys()](../reference/Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys) return a native iterator, not a generator. Two threads calling `next()` on the same one at once get `ValueError` at once, as with a shared generator.


## FanoutCache (D-032 to D-040)

These are covered together in [FanoutCache: sharded caches](fanoutcache.md), and listed against diskcache's in [Switching from python-diskcache](switching-from-diskcache.md#fanoutcache).

- **D-032:** the shard count is part of where every key lives, so it's recorded, and a different `shards=` is an error instead of a quiet loss of data and atomicity.
- **D-033:** the hash is fixed by its definition and part of the format, and `iterkeys(prefix=)` gives [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) the ordered listing [Cache](../reference/Cache.md#diskcache_oxide.Cache) has.
- **D-034:** a silently dropped write and a hang are both worse than an error the caller chose how to handle; `durable=True` is that choice.
- **D-035:** a maintenance job must finish in bounded time, and when it can't, it must say what it did.
- **D-036 and D-037:** budgets (size, connections, file descriptors) follow configuration, not the number of threads or shards.
- **D-038:** a typo shouldn't leave a directory that neither kind of cache can open, or a shard written behind the [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache)'s back.
- **D-039:** the missing stock features each need their own design across shards, so they arrive with Phase 6a and 6d rather than as copies.
- **D-040:** holding every shard's lock would serialize the whole cache behind one transaction, which is what sharding exists to avoid.


# diskcache-community

[diskcache-community](https://github.com/secondwindoss/diskcache-community) is an actively maintained fork of diskcache, and the conformance suite can run against it too. It has fixed some of the same bugs (D-012, D-018, D-019, and the `0700` directories of D-015) and keeps others (D-008, D-016, D-017). The full comparison is at the end of [tests/DIVERGENCES.md](https://github.com/mikeckennedy/diskcache-oxide/blob/main/tests/DIVERGENCES.md).
