# Storage and WAL

Nobody restarts a cache. It sits inside a worker process for weeks, and whatever it does to memory and disk, it does slowly and out of sight, until an alert fires at a bad hour. So how much a cache uses has to be something you set, not something you discover. This page covers what diskcache-oxide puts on disk, and the settings that keep memory and disk where you put them.


# What's in the directory

Open the directory and this is what you find:

    /var/cache/my-app/
    ├── diskcache-oxide.sqlite        # keys, tags, settings and small values
    ├── diskcache-oxide.sqlite-wal    # SQLite's write-ahead log
    ├── diskcache-oxide.sqlite-shm    # SQLite's shared-memory index for the log
    └── 3f/
        └── 3fa2…c91e.val             # one large value

A [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) directory holds a small manifest file, `diskcache-oxide-fanout`, and one numbered folder per shard (`000`, `001`, …), each laid out like the directory above.

The database file carries a format version, and a build that can't read it raises [FormatMismatchError](../reference/FormatMismatchError.md#diskcache_oxide.FormatMismatchError) rather than guessing. Nothing in the directory is read or changed when that happens. During 0.x, a release that changes the format bumps the minor version and says so first in its changelog entry, and existing caches are deleted on that upgrade, not migrated.

0.2.3 writes format 3, which added tags and the eviction policy. Caches made by 0.1.2 are format 2, so after the upgrade, opening one raises [FormatMismatchError](../reference/FormatMismatchError.md#diskcache_oxide.FormatMismatchError) naming the file. Stop every process using the cache, delete its directory (for a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache), the whole root, never one shard), and the next open creates it empty. Don't delete only the `.sqlite` file, since its value files would be left behind. 0.1.2 refuses a format-3 cache the same way. See [Upgrading diskcache-oxide](switching-from-diskcache.md#upgrading-diskcache-oxide).


# Small values inline, large values in files

- **Under 64 KiB** (after encoding), a value is stored in its database row. Reading it is one indexed lookup.
- **64 KiB or more**, it's written to its own file, and the row points to it. This keeps the database small and its page cache hot, and large values stream straight from the file.

Writing a large value never holds the database lock while the bytes go to disk: the value goes to a temporary file first, then inside a short transaction the file is renamed into place and the row updated. The old file is removed after the commit, and file names are never reused.

Large values live one folder deep, in up to 256 folders named by the first two characters of the file's name (D-042). Deleting a value removes only its file. When the process closes its last handle on the cache, it sweeps those folders: a temp file that no writer has touched for an hour, left by a process killed mid-write, is removed, and a folder left empty goes too, skipping any touched in the last 30 seconds so a busy folder isn't removed and re-created, all within 100 ms; a close that runs out of time leaves the rest to a later one. [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) runs the same sweep and removes the folders it emptied at once. So a cache never has more than 256 value folders, and a closed one has none sitting empty for long. A [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache)'s shards don't sweep yet: each keeps at most 256.

A reader racing an overwrite always gets a whole value, old or new. If a value's file really is gone (deleted by hand, or cut short by a crash, since value files aren't fsynced), [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) returns the default and the orphaned row is cleaned up, so `in` and `len` don't keep reporting an entry that can't be read.


# Size limit and eviction

``` python
cache = Cache('/var/cache/my-app', size_limit=500 * 1024 * 1024, cull_limit=10)
```

- [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) is the cache's disk budget in bytes: the database plus value files. The default is 1 GiB and the minimum is 1 MiB.
- When a [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) finds the cache over its limit, it evicts up to [cull_limit](../reference/Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit) of the least recently stored entries (default 10). [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) evicts down to the limit on demand.
- [volume()](../reference/Cache.volume.md#diskcache_oxide.Cache.volume) reports the current size.

Least-recently-stored eviction means a [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) never has to write anything, which keeps reads fast and lets any number of processes read in parallel. LRU and LFU aren't supported, because they make every [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) a write: `'least-recently-used'` and `'least-frequently-used'` raise `ValueError` (D-022, and see the [roadmap](roadmap.md)). The hit and miss counts from [stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats) follow the same rule. They're kept in memory, per process, and never written to the database.


## `eviction_policy='none'`

Some entries mustn't disappear just because the cache got full: a lock, a job claim, the items in a work queue. For a cache that holds those, turn eviction off:

``` python
locks = Cache('/var/cache/my-app-locks', eviction_policy='none')
```

Under `'none'`, no write and no [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) evicts for size. Expired entries are still removed: each write sweeps up to [cull_limit](../reference/Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit) of them, [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) and [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) remove the rest, and [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) returns how many it removed. The cache can grow past [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit), so give the entries an [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) and keep them apart from your general-purpose cache, which should go on evicting.

The policy and both limits are stored in the cache, so every process shares them. Each write reads the policy afresh, so a change made from one process applies to every other at its next write, and a handle opened with `eviction_policy=None` (the default) keeps whatever is stored. [reset()](../reference/Cache.reset.md#diskcache_oxide.Cache.reset) reads or changes one of them without reopening:

``` python
cache.reset('eviction_policy')            # 'least-recently-stored'
cache.reset('size_limit', 2 * 2**30)      # stored, and returned
```


# Tags

A tag from `set(..., tag=...)` is stored in the entry's own row. A partial index covers only the rows that have one, so untagged entries cost nothing extra, and `evict(tag)` finds a tag's entries through the index instead of scanning the cache. Like [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), it works in chunks and removes the entries that had the tag when it started, leaving ones written while it runs.


# Memory: `max_connections` and `sqlite_cache_size`

diskcache gives each thread its own connection with a 32 MiB page cache plus a 64 MiB memory map, so its memory grows with your thread count. diskcache-oxide bounds it:

``` python
cache = Cache(
    '/var/cache/my-app',
    max_connections=8,       # connections per database file, per process (default 8)
    sqlite_cache_size=256,   # 4 KiB pages per connection: 1 MiB (default)
)
```

SQLite's memory for a directory stays around `max_connections × sqlite_cache_size` pages, 8 MiB per process by default, however many threads use it. The 1 MiB default is the smallest size that left hot reads, writes and the threaded benchmarks unchanged. If your cache is large and read uniformly at random, raising it helps: 2,000 pages measured about 17% faster on that workload.


# The write-ahead log

SQLite's write-ahead log (WAL) is a ledger of recent changes kept beside the database, and it's what lets readers and a writer work at the same time: the writer appends to the ledger while readers keep reading the book. Every so often SQLite folds the ledger back into the book and starts over. The catch in a multi-process app is that it can only start over when it finds a moment with no readers, and with readers in other processes that moment may never come. A burst of writes (a big fill, or a [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear)) grows the log to fit the burst, and then it stays that size. diskcache-oxide's soak tests saw logs over 1 GiB next to databases of a few MiB.

diskcache-oxide handles this in layers, from free and always on to explicit.


## Always on: `wal_size_limit`

``` python
cache = Cache('/var/cache/my-app', wal_size_limit=64 * 1024 * 1024)   # the default
```

This tells SQLite to trim the log back to half the limit (32 MiB by default) whenever it resets, and scales how often SQLite checkpoints automatically. These are connection settings, so they never pause anything. At normal load they keep the log at a few MiB.


## On demand: [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint)

``` python
cache.checkpoint()   # True if the log was reset and truncated
```

[checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) copies the log into the database and truncates it to zero. It waits briefly for in-flight reads, retrying up to the cache's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout), and returns `False` instead of raising if other processes keep the database busy. Call it when a short pause suits your app: at shutdown, in a maintenance job, or after a large [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear).


## At close

[close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) runs the same checkpoint when this is the process's last handle on the cache and the log is over half the limit, waiting at most a second. A small log, the usual case, costs nothing extra. Pass `close(checkpoint=False)` to skip it.


## Opt-in: automatic checkpoints

``` python
cache = Cache('/var/cache/my-app', wal_checkpoint='auto')
```

With `'auto'`, writes check the log (at most every 250 ms or 64 writes) and checkpoint it when it's over the limit, with the interpreter released. Readers in other processes can get in the way for a moment, so it tries the final truncation up to 8 times, a couple of milliseconds each, before backing off for a second. It never fails a write. It isn't the default because on a saturated disk the write that triggers the checkpoint can still pause for a long time, and no write should pay for housekeeping unless you asked for it. The plan is to move this into a background maintenance thread so writes never pay for it at all. Writes queue behind a checkpoint for tens of milliseconds, so a [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) under about 35 ms can time out behind one.


## The limit is a target, not a bound

`wal_size_limit` says where the log should stay, not where it's guaranteed to stay. Sustained writes from several processes near flat out can take it to tens of times the limit: we measured 6 GiB on free-threaded 3.15 and 21 GiB on 3.14 in 10 minutes, against a 64 MiB limit. It's much smaller below flat out, for example 250 MiB at about 2,600 writes per second across 3 writers in `auto`.

In the default `manual` mode, with busy readers in other processes, the log grows until something checkpoints. If your app has that shape, use `wal_checkpoint='auto'` or call [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) periodically. A script should call [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) or [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) after a large fill.


# Maintenance calls

All of these work in chunks, release the database between chunks so other writers get in, and are safe to call while other processes use the cache:

- [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) removes every expired entry and its value file.
- [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) removes every entry that existed when it started, and keeps writes that land while it runs.
- [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) removes expired entries, then evicts down to [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) (nothing under `eviction_policy='none'`).
- `evict(tag)` removes every entry that had the tag when it started.

[check()](../reference/Cache.check.md#diskcache_oxide.Cache.check) compares the database with the value files and returns what doesn't match, as a list of `warnings.WarningMessage`: SQLite's integrity check, entries whose value file is missing or the wrong size, files older than an hour that no entry names, and stored totals that don't add up. It runs in short steps, apart from SQLite's integrity check. `check(fix=True)` also repairs what it found: it removes the broken entries and the unknown files in value folders, sweeps empty value folders, corrects the totals, then runs `VACUUM`. `VACUUM` rewrites the whole database, needs room for a temporary copy of it in `SQLITE_TMPDIR`, `TMPDIR` or `/tmp`, and makes writers and forks wait while it runs, so run `fix=True` in a maintenance window, not from a request.
