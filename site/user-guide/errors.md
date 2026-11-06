# Errors and what to do about them

Sooner or later a traceback ends in `diskcache_oxide`, and the question is whether it's your bug, an outage, or two processes disagreeing about what a directory is for. diskcache-oxide raises when something is wrong rather than guessing, and each exception is chosen so that `except` clauses you already have keep working: [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) is a `TimeoutError`, [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) is a `TypeError`, [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError) is a `ValueError`. This page is the field guide: every exception, what it means, and what to do. The last section covers the log lines the library writes when it has been told not to raise, because then a log line is the only evidence you'll get.

| Exception | Base classes | Where |
|----|----|----|
| [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) | `TimeoutError` (so `OSError`) | any call that waits for a lock or a fork |
| [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy) | [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) | [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) on a second cache |
| [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) | `TypeError` | reading a pickled value without `pickle=True` |
| [CorruptValueError](../reference/CorruptValueError.md#diskcache_oxide.CorruptValueError) | `ValueError` | reading a value no build could decode |
| [ForeignValueError](../reference/ForeignValueError.md#diskcache_oxide.ForeignValueError) | [CorruptValueError](../reference/CorruptValueError.md#diskcache_oxide.CorruptValueError) | reading a value another version or host could decode |
| [FormatMismatchError](../reference/FormatMismatchError.md#diskcache_oxide.FormatMismatchError) | `RuntimeError` | opening a database or manifest this version can't read |
| [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError) | [FormatMismatchError](../reference/FormatMismatchError.md#diskcache_oxide.FormatMismatchError), `ValueError` | opening a directory as the wrong kind of cache |
| `TypeError`, `ValueError`, `KeyError`, `RuntimeError`, `OSError`, `MemoryError` | built-in | described below |


# `TypeError` at [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set): a value that can't be stored

``` text
TypeError: values of type Episode can't be stored without pickle (only exact built-in types round-trip)
```

The value isn't one the msgpack or JSON format can represent exactly, so it's refused where it was created rather than three requests later when something reads it. Only exact built-in types round-trip: `str`, `bytes`, `int`, `float`, `bool`, `None`, and `list`, `tuple`, `dict`, [set](../reference/Cache.set.md#diskcache_oxide.Cache.set) and `datetime` made of those. A dataclass, an ORM row, a `dict` subclass or a class of your own doesn't.

Fix it in this order:

1.  Store a plain structure instead: a dict from `dataclasses.asdict()` or a model's `model_dump()`, and rebuild the object on the way out. This is the right answer for almost every cache entry, and it keeps pickle out of the directory.
2.  If you really need arbitrary objects, open a separate cache for them with `Cache('/var/cache/my-app-objects', pickle=True)`. Don't turn `pickle=True` on for a cache that other code shares just to make the error go away: every reader of that directory then runs `pickle.loads` on whatever is written there. See [Serialization](serialization.md).


# `TypeError` for keys and booleans

``` text
TypeError: cache keys must be str, not int
TypeError: expire must be a number of seconds or None, not bool
```

Keys are `str` only for now; build them yourself (`f'user:{user_id}'`). Other key types are planned and won't need a format change. A `bool` where a number of seconds belongs (`expire=True`, `timeout=True`) is almost always an argument in the wrong position, so it's refused rather than treated as `1`, which is what diskcache does.


# [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout): a lock or a fork outlasted [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout)

``` text
diskcache_oxide.Timeout: timed out after 1 s waiting for a lock or a pending fork
```

The call waited the cache's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) (60 s by default, or the `timeout=` passed to that call) and gave up. Something held what it needed, and there are three suspects:

- **Another writer held the write lock** that long. One process at a time commits to a database file, and a burst of writes, a [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) from a maintenance script, or a long [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) block holds it. Reads never wait for writers, so a [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) on a [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) is one of the next two cases.
- **A fork was pending.** Before a fork, every cache call in the process waits for in-flight writes and then for the fork itself, and that counts against [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout). A fork takes about a millisecond in a quiet process and tens to hundreds of milliseconds beside busy threads on a GIL build. A [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) below that sees [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) during ordinary forks. See [How long a call waits for a fork](concurrency-and-forks.md#how-long-a-call-waits-for-a-fork).
- **Reading a large value kept losing to overwrites** of the same key until [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout). Rare, and only on a key that's rewritten flat out.

What to do:

- **In a request path, catch it and skip the cache.** Give the cache a short [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) (`Cache(path, timeout=1.0)`), and treat [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) as "don't cache this time": the value is a copy, and the request can go to the source of truth. [Sharing a cache across web workers](../recipes/web-workers.md) has the pattern.
- **Give maintenance its own timeout.** [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) and [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) take `timeout=` for that call, and bound each chunk's wait, so an admin page can pass `clear(timeout=10)` on a cache whose requests use 1 second. Run them from one scheduled job rather than from every worker.
- **Keep [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) blocks short**, and never `await` inside one.
- **If many processes write often, use a [`FanoutCache`](fanoutcache.md).** A plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) has one write lock per directory, and eight workers that all write queue on it. Sharding gives each shard its own.
- **Let the library answer it as a miss** with [`durable=True`](getting-started.md#a-cache-that-cant-take-a-request-down): single-key calls then return their miss value instead of raising, and the failure is counted and logged. Pair it with a small [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout), since it doesn't bound time on its own.
- [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) never raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout): it returns `False` when other processes keep it busy. Call it again later, or at shutdown.


# [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy): two caches in one transaction

Inside one cache's [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) block, beginning a transaction on a second cache whose write lock another thread holds raises [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy) at once instead of waiting. That thread could be waiting for the lock you hold, and then neither would ever finish. It's a [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) subclass, so existing handlers catch it, and `retry=True` never retries it.

Nest the two caches in the same order everywhere in your app. When [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy) reaches you, leave the outer block, so your lock is released, and try the whole thing again. See [Transactions](transactions.md#two-caches-at-once-transactionbusy).


# `RuntimeError` from a transaction

``` text
RuntimeError: the write transaction was rolled back after an error; nothing in it can commit
```

A `with cache.transact():` block never exits cleanly when its writes are gone. One of its operations failed in a way that left the transaction unusable (a full disk, an I/O error), another thread rolled it back, or it was ended from inside one of its own operations. Nothing in the block was kept. Treat it as you would the original failure: log it, and run the block again if the work is idempotent.

``` text
RuntimeError: this database can't be written in a process forked inside a transaction
```

A process forked while the forking thread itself held a [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) block, and this is the child. The parent's transaction carries on and commits; the child's copy is poisoned, and its writes to that database raise this instead of stalling for a minute. Don't fork from inside a transaction: start worker processes before the block, or after it. See [Forking from inside your own transaction](concurrency-and-forks.md#forking-from-inside-your-own-transaction).


# [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError): a pickled value, read without `pickle=True`

``` text
UnsafeValueError: this value was stored with pickle; open the cache with pickle=True to read it
```

Two openers of one directory disagree about what it's allowed to hold. A handle opened without `pickle=True` found a row that a `pickle=True` handle wrote, and refused to unpickle it, because unpickling runs whatever code the bytes describe. The row is left in place for the handle that can read it. This is a configuration mismatch, not damage, so it raises under every `on_decode_error` policy and under `durable=True` too.

Decide which the directory is for. If it holds pickled objects on purpose, open every handle on it with `pickle=True`. If it shouldn't, find the writer that passed `pickle=True` and give it its own directory, then [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) this one. It's a `TypeError`, like the error at [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) above, because both come from the same decision: what this cache is allowed to hold.


# [CorruptValueError](../reference/CorruptValueError.md#diskcache_oxide.CorruptValueError) and [ForeignValueError](../reference/ForeignValueError.md#diskcache_oxide.ForeignValueError): a value that can't be decoded

``` text
ForeignValueError: ...
CorruptValueError: ...
```

[CorruptValueError](../reference/CorruptValueError.md#diskcache_oxide.CorruptValueError) means no build of diskcache-oxide could read the stored bytes: they're malformed, or a pickled class has been removed since it was stored. [ForeignValueError](../reference/ForeignValueError.md#diskcache_oxide.ForeignValueError), its subclass, means this build can't read them but another might: a newer version wrote a mode or tag this one doesn't know, or the value needs a time zone this host doesn't have.

By default a read raises these, as diskcache does. A cache holds copies, so most apps would rather treat them as a miss:

- `Cache(..., on_decode_error='miss')` returns the default, logs a warning on the `diskcache_oxide` logger, and deletes a row no build could read. A foreign row is kept, since the build that wrote it can still read it. `durable=True` turns this on by default.
- Pass a function instead to report it yourself: `on_decode_error=lambda key, exc: sentry.capture(exc)`. From a [Caches](../reference/Caches.md#diskcache_oxide.Caches) registry it's called as `(name, key, exc)`.
- For a [ForeignValueError](../reference/ForeignValueError.md#diskcache_oxide.ForeignValueError) that keeps coming back, upgrade the older readers to the version that wrote the value, or stop the newer writer from sharing the directory. For a missing time zone, install the host's zone data.

The full table of what each policy does with each failure is in [Values that can't be read](getting-started.md#values-that-cant-be-read).


# [FormatMismatchError](../reference/FormatMismatchError.md#diskcache_oxide.FormatMismatchError): a database this version can't read

``` text
FormatMismatchError: cache format version 3 is not supported (this build uses version 2) at /var/cache/my-app
```

The directory holds a database, or a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) manifest, written in a format this build doesn't know, usually by a newer diskcache-oxide. Nothing is read or changed. Upgrade this process to match the writer, or point it at a different directory. If the directory should be reset, stop every process using it and delete it; a cache starts cold without harm.


# [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError): the wrong kind of cache for the directory

``` text
LayoutMismatchError: the root has 8 shards, not 16
```

The directory's layout doesn't match how it was opened: a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) on a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) root or one of its shard directories, a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) on a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache)'s directory, or a `shards=` count that differs from the one the root records. The refused open creates nothing, so a typo doesn't leave a directory neither kind can open. It's both a `ValueError` and a [FormatMismatchError](../reference/FormatMismatchError.md#diskcache_oxide.FormatMismatchError).

Use the class the directory was created with, and for a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) either leave [shards](../reference/FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) out (the stored count is used) or pass the stored one. A different shard count means a new directory: the count decides where every key lives, so it can't change in place. See [The layout and the hash](fanoutcache.md#the-layout-and-the-hash). In a [`Caches`](caches.md) registry, a lookup that hits this returns a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) and reports it like any failed open.


# `RuntimeError`: the filesystem can't run SQLite in WAL mode

``` text
RuntimeError: the cache requires WAL journal mode, but SQLite is using "delete"
```

SQLite refused write-ahead logging, which diskcache-oxide requires for readers and a writer to work at the same time. That happens on network filesystems (NFS, SMB) and some FUSE mounts. The cache has to live on a local disk: `/var/cache/<app>`, or a Docker volume on the host's disk. See [Pick a directory](../recipes/web-workers.md#pick-a-directory).


# `OSError`

- **Too many open files (`errno` `EMFILE`) opening a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache).** Each shard takes about 4 file descriptors to open, so 256 shards need more than Linux's usual `ulimit -n` of 1,024. The message gives the shard count, the descriptors needed, the limit and how many are already open. Raise the limit, or use fewer shards; eight is plenty for most apps. A failed open leaves nothing behind.
- **No hard links.** Creating a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) manifest uses a hard link, so a filesystem without them (FAT, exFAT, SMB, some FUSE mounts) raises `OSError` saying so. Use a local disk.
- **Disk full, or an I/O error.** The write didn't happen and the cache is unchanged. Under `durable=True` a single-key call answers it as a miss or `False` and logs it; otherwise it raises. [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) is an `OSError` too, so an `except OSError` around a cache call catches both.


# `ValueError`

- [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) below 1 MiB. In diskcache a tiny limit silently evicts every value, including the one just stored; here it's refused. For a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) the total must be at least [shards](../reference/FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) MiB, since each shard needs 1 MiB.
- `max_connections` below [shards](../reference/FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) on a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache). Each shard needs at least one connection.
- A key on another shard inside `transact(key=...)`. The block covers one shard, and a call for a key elsewhere, reads included, can't be atomic with it. Check with [shard_of()](../reference/FanoutCache.shard_of.md#diskcache_oxide.FanoutCache.shard_of) that the keys share a shard, or keep them in one value.
- A [Caches](../reference/Caches.md#diskcache_oxide.Caches) name that's taken, isn't a plain directory name, or would share a directory with another registered cache.


# `KeyError`

- `cache[key]` and `del cache[key]` for a missing or expired key, as with a dict. Use [get()](../reference/Cache.get.md#diskcache_oxide.Cache.get) and [delete()](../reference/Cache.delete.md#diskcache_oxide.Cache.delete) for the "might not be there" cases.
- `incr(key, default=None)` and `decr(key, default=None)` on a missing key. With the default `default=0`, a missing key starts counting from 0 instead.
- Every `NullCache[key]`, since nothing is ever present.


# `MemoryError`

It always raises, under `durable=True` too, including when SQLite runs out of memory. A [Caches](../reference/Caches.md#diskcache_oxide.Caches) lookup raises it as well, rather than handing out a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache).


# Log lines, when the library was told not to raise

Three settings turn failures into log lines on the `diskcache_oxide` logger instead of exceptions. Make sure that logger reaches your logging setup, since the line is then the only signal.

- **`on_decode_error='miss'`** logs a warning for each stored version it couldn't read, once per cache handle, and deletes the row if no build could read it.
- **`durable=True`** counts every swallowed failure in one of three groups and logs each group's first failure at once, then at most once a minute per cache handle, with counts by operation since its last line (`get: 1204, set: 37`). Busy (a [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout)) logs at `WARNING`, failing (a full disk, an I/O error, a damaged database) at `ERROR`, and lost invalidations (any failed [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete), whose entry can now be served stale) at `ERROR`. An outage that ends within the minute shows only its first failure until the next line, so each line says more may follow. [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) logs what's left.
- **[Caches](../reference/Caches.md#diskcache_oxide.Caches)** logs `could not open cache 'pages' at /var/cache/my-app/pages; using a NullCache, retrying in 10 s` at `ERROR` with the traceback when a cache can't open, and the same at `WARNING` without one when the open merely timed out (that's retried after at most 1 s). Pass `on_open_error=` to send it to Sentry instead.


# If it looks like a bug in diskcache-oxide

If you've read this far and the traceback still doesn't fit, it may be one. Open an [issue](https://github.com/mikeckennedy/diskcache-oxide/issues) with `diskcache_oxide.__version__`, [diskcache_oxide.sqlite_version()](../reference/sqlite_version.md#diskcache_oxide.sqlite_version), the interpreter (`python -VV` says whether it's free-threaded), the platform and filesystem, how many processes and threads share the directory, and the full traceback. Every deliberate difference from diskcache is listed in [Divergences](divergences.md), so check there first if the surprise is a behavior rather than an error.
