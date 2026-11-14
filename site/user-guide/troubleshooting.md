# Troubleshooting

[Errors and what to do about them](errors.md) starts from an exception. This page starts from what you see. Each section lists the usual causes, most likely first, and links the page with the details.


# Every lookup misses

- **The registry handed you a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache).** A [`Caches`](caches.md) lookup returns one when the cache can't open, and while it's disabled. Check `caches[name].enabled` (False on a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache)) and `caches.stats()[name].error`, and look for the `could not open cache` line at `ERROR` on the `diskcache_oxide` logger.
- **A decorator bound a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) at import.** `@caches['x'].memoize()` looks the cache up once, when the module is imported, and keeps what it got. If the cache couldn't open then, the function runs uncached until a restart. Use the [`memoized(name)` helper](caches.md#memoize-on-a-registered-cache).
- **The values are there but this process can't read them.** A value of a [registered type](serialization.md#your-own-types) reads as a miss in a process that hasn't registered that type, when the cache has `on_decode_error='miss'` (the default under `durable=True`), with a warning naming the tag. Register your types in a module every entry point imports: the web app, the worker, scripts.
- **The workers aren't opening the same directory.** A relative path resolves against each process's working directory, which differs between a web server, a worker and a cron job. Use absolute paths.
- **The cache moved after a format change.** A release that changed the on-disk format opens its own cache in `diskcache-oxide-format-N` and starts empty, with a `WARNING` saying so. It warms up again. See [Upgrading across a format change](../recipes/upgrading.md).
- **Time isn't what the cache thinks.** In tests, patching `time.time` doesn't move the cache's clock; pass `clock=`. See [Testing](../recipes/testing.md).
- **The entries really are gone.** A [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) too small for the working set evicts entries soon after they're written. Compare [volume()](../reference/Cache.volume.md#diskcache_oxide.Cache.volume) with [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit), and the hit rate from [stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats).


# The disk keeps growing

- **The write-ahead log.** Each cache has a `-wal` file beside its database, trimmed back to `wal_size_limit / 2` when it resets. After a burst of writes it can be much larger than the database until [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) runs. See [Storage and WAL](storage-and-wal.md).
- **Eviction is off.** A cache with `eviction_policy='none'` (the recommended setting for sessions, claims and queues) holds what you put in until it expires, past [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) if need be. Give those entries an [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire), and run [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) from a scheduled job so expired ones free their space.
- **Expired entries that nobody removed.** Expired entries are invisible to reads at once, but their space comes back only when [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) or a later write's cleanup removes them. On a cache that's mostly read, run [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) (or `caches.expire_all()`) hourly.
- **An old format's cache.** After a format change the old cache stays on disk beside the new one until you run [remove_old_formats()](../reference/Cache.remove_old_formats.md#diskcache_oxide.Cache.remove_old_formats).
- **Every cache adds up.** A volume holds the sum of the [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit)s, about one write-ahead log per cache, and the unbounded state caches. A [`FanoutCache`](fanoutcache.md) has a log per shard. [Docker and Compose](../recipes/docker.md#how-much-disk) has the arithmetic.
- **Files no entry names.** A crash at the wrong moment can leave a large value's file behind. `check(fix=True)` removes value files no entry names once they're an hour old.


# Writes are slow under load

- **Many processes writing one cache.** A cache has one write lock; reads never wait for it, writes queue for it. If one cache that many processes write (counters, rate limits) shows [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) warnings or slow writes, make it a [`FanoutCache`](fanoutcache.md), which gives each shard its own lock.
- **A big maintenance call.** [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) or [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) on a large cache takes the write lock in chunks and steps aside between them, but a write can still wait behind one. Run maintenance from one scheduled job, with its own `timeout=`.
- **A transaction held too long.** Everything inside `with cache.transact():` holds the write lock. Keep transactions short and do slow work outside them. See [Transactions](transactions.md).
- **A slow disk.** A cache on a slow, emulated or network filesystem writes and checkpoints slowly, and on a network filesystem it isn't safe at all: keep it on a local disk.


# `PermissionError`

- **Another user created the directory.** Cache directories are created owner-only (`0700`), so only the user that created one can open it. A script run with `sudo`, a root cron job, or a `docker exec -u root` that touched the directory first leaves it owned by root. Fix the ownership (`chown -R`), and run everything that shares a cache as one user. See [Open a cache](getting-started.md#open-a-cache).
- **A container with a different UID.** Every container that shares a volume must run as the same user, and that user must never change between image versions. See [Docker and Compose](../recipes/docker.md#who-owns-the-directory).
- **Windows: the files are open.** Windows won't delete a file a process has open, so removing a cache directory while a handle is open fails. [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) every handle first. See [Windows](getting-started.md#windows).


# Which format is this cache in?

`cache.directory` ends in `diskcache-oxide-format-N` when the cache was opened in a versioned subdirectory after a format change; otherwise it's the directory you passed, in this release's format. To read a database's format without opening it as a cache, use the standard library's `sqlite3`, read-only, from a separate process (a shell or a script), never from one that has the cache open: closing a second SQLite library's connection to the same file can drop the locks the cache holds on it.

``` python
import sqlite3

con = sqlite3.connect('file:/var/lib/my-app/cache/pages/diskcache-oxide.sqlite?mode=ro', uri=True)
print(con.execute('PRAGMA user_version').fetchone()[0])  # 3 for 0.2.3 and later
con.close()
```


# Still stuck

Run `cache.check()`: it reports damage to the database, value files that don't match their entries, and totals that don't add up. If it looks like a bug in diskcache-oxide, see [If it looks like a bug](errors.md#if-it-looks-like-a-bug-in-diskcache-oxide).
