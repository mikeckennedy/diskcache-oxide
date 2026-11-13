# Watching a cache in production

A cache runs inside your workers for weeks without anyone looking at it, until a page gets slow or the disk fills up and someone asks whether the cache is working. Is it full? Is anything hitting it? Is the write-ahead log eating the disk? The numbers that answer those questions are cheap to read, so put them on a health endpoint or an admin page before you need them.


# A health check

Everything here reads a few numbers and never waits on writers, so it's fine to call on every health probe:


``` python
import os
import tempfile

from diskcache_oxide import Cache, CacheLike

cache = Cache(tempfile.mkdtemp(), size_limit=64 * 2**20)
cache.stats(enable=True)  # at startup, in every worker: counting is off until something turns it on

def wal_bytes(cache: CacheLike) -> int:
    if cache.directory is None:  # a NullCache
        return 0
    try:
        return os.path.getsize(os.path.join(cache.directory, 'diskcache-oxide.sqlite-wal'))
    except FileNotFoundError:
        return 0

def cache_health(cache: CacheLike) -> dict:
    hits, misses = cache.stats()
    lookups = hits + misses
    volume = cache.volume()
    return {
        'enabled': cache.enabled,
        'entries': len(cache),
        'volume_mib': round(volume / 2**20, 1),
        'percent_of_limit': round(100 * volume / cache.size_limit, 1),
        'eviction_policy': cache.eviction_policy,
        'hits': hits,
        'misses': misses,
        'hit_ratio': round(hits / lookups, 2) if lookups else None,
        'wal_mib': round(wal_bytes(cache) / 2**20, 1),
    }

for n in range(200):
    cache.set(f'page:{n}', 'x' * 20_000)
for n in range(300):
    cache.get(f'page:{n}')

print(cache_health(cache))
```


    {'enabled': True, 'entries': 200, 'volume_mib': 3.9, 'percent_of_limit': 6.2, 'eviction_policy': 'least-recently-stored', 'hits': 200, 'misses': 100, 'hit_ratio': 0.67, 'wal_mib': 3.9}


Return it from an endpoint and you have something to graph or alert on. In Flask:

``` python
@app.get('/health/cache')
def health_cache() -> dict:
    return cache_health(cache)
```

In FastAPI the same function body works under `@app.get('/health/cache')`. Keep the endpoint behind your admin auth or on an internal port: the numbers aren't secret, but there's no reason to publish them.


# What the numbers mean

- **[enabled](../reference/Cache.enabled.md#diskcache_oxide.Cache.enabled)** is False for a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache). If the cache comes from a [`Caches`](../user-guide/caches.md) registry, that's what a lookup hands out while the cache can't open, so `enabled: false` on a cache you never switched off means something is wrong. The log says what.
- **`len(cache)`** counts entries, including expired ones not yet removed. It's read from a stored total, so it costs the same on a cache of ten entries or ten million.
- **[volume()](../reference/Cache.volume.md#diskcache_oxide.Cache.volume)** is the cache's size in bytes: the database pages in use plus the value files. It leaves out the write-ahead log and pages freed by deletes, which SQLite keeps in the file for reuse, so `du` on the directory can show more. `check(fix=True)` (below) gives the freed pages back.
- **[size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) and [eviction_policy](../reference/Cache.eviction_policy.md#diskcache_oxide.Cache.eviction_policy).** Under the default `'least-recently-stored'`, a full cache sits close to its limit, and that's normal: each [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) that finds it over the limit evicts the oldest entries. Under `'none'` nothing is evicted for size, so `percent_of_limit` can pass 100. On a [sessions](sessions.md) or [work queue](work-queues.md) cache, that's the number to alert on. Both attributes are the values this process last saw; `cache.reset('size_limit')` and `cache.reset('eviction_policy')` read what's stored now, in case another process changed them.
- **Hits and misses** count [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) and `cache[key]` in this process. An expired entry, and a value answered as a miss by `on_decode_error`, count as misses. `in`, [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) and iteration don't count, and a `durable=True` outage answered as a miss counts as nothing. A [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) sums its shards, and a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) always reports `(0, 0)`.

A low hit ratio on a full `'least-recently-stored'` cache can mean [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) is too small for what you're caching: entries are evicted before anyone reads them twice. Raise it with `cache.reset('size_limit', 2 * 2**30)`, which every process picks up at its next write, or in the constructor for the next deploy.


# Hits and misses across workers

The counts live in each process's memory and are never written to the database, so reading them costs nothing. That also means a health probe that lands on one gunicorn worker sees that worker's counts only, since its last restart; a forked child starts from zero. Every handle on one directory in a process shares one set of counts, and they survive closing the last handle.

For a total across workers, have each worker add its counts to a shared counter now and then, from a timer or after every few hundred requests. Keep those counters in a small cache of their own, so reading the totals doesn't count as lookups in the cache you're measuring:


``` python
counters = Cache(tempfile.mkdtemp())

def publish_stats(cache: Cache, counters: Cache) -> None:
    hits, misses = cache.stats(reset=True)
    counters.incr('pages:hits', hits)
    counters.incr('pages:misses', misses)

publish_stats(cache, counters)
cache.get('page:1')
publish_stats(cache, counters)
print('every worker:', counters.get('pages:hits'), 'hits,', counters.get('pages:misses'), 'misses')
print('this worker since:', cache.stats())
```


    every worker: 201 hits, 100 misses
    this worker since: (0, 0)


`stats(reset=True)` returns the counts and zeroes them in one call, so a lookup between the read and the reset isn't lost. This is the [counters](counters.md) recipe applied to the cache itself.


# An admin page for every cache

A [Caches](../reference/Caches.md#diskcache_oxide.Caches) registry gives you one row per cache with [stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats). It opens each registered cache, and never raises: a cache that can't be opened gets its `error` set, with the exception's type and message.


``` python
import dataclasses

from diskcache_oxide import Caches

caches = Caches(tempfile.mkdtemp(), timeout=1.0)
caches.register('general')
caches.register('pages', size_limit=256 * 2**20)
caches['general'].set('greeting', 'hello')

for name, stat in caches.stats().items():
    print(dataclasses.asdict(stat) | {'directory': '...'})
```


    {'name': 'general', 'directory': '...', 'enabled': True, 'count': 1, 'volume': 40960, 'error': None}
    {'name': 'pages', 'directory': '...', 'enabled': True, 'count': 0, 'volume': 40960, 'error': None}


A cache that can't open shows up here with `count` and [volume](../reference/Cache.volume.md#diskcache_oxide.Cache.volume) at 0 and the reason in `error`, for example `FormatMismatchError: cache format version 2 is not supported (this build uses version 3) ...` after an upgrade that changed the format (see [Upgrading across a format change](upgrading.md)).

As an endpoint, with each cache's hit counts added. Every cache in this registry is a plain one; read a sharded one with `caches.fanout(name)` instead, since `caches[name]` raises `TypeError` for it:

``` python
@app.get('/admin/caches')
def admin_caches() -> dict:
    rows = {}
    for name, stat in caches.stats().items():
        hits, misses = caches[name].stats()
        rows[name] = dataclasses.asdict(stat) | {'hits': hits, 'misses': misses}
    return rows

@app.post('/admin/caches/<name>/clear')
def clear_cache(name: str) -> dict:
    return {'removed': caches.clear(name, timeout=10)}
```

`caches.clear()` raises if the cache can't be opened, so the page never reports "0 removed" for a failure. It works on a disabled cache too. Give it a longer `timeout=` than the request path's, since it holds the write lock once per chunk. For the rest of the registry's admin calls, see [Admin and shutdown](../user-guide/caches.md#admin-and-shutdown).


# The write-ahead log

`wal_mib` in the health check is the size of `diskcache-oxide.sqlite-wal` beside the database. At normal load `wal_size_limit` keeps it at a few MiB. A burst of writes with readers in other processes can leave it much larger, and in the default `wal_checkpoint='manual'` mode it stays that size until something checkpoints it.

If it keeps growing, call [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) from a maintenance job. It returns True when the log was copied into the database and truncated, and False, without raising, when other processes kept it busy; try again later. If that isn't enough, open the cache with `wal_checkpoint='auto'`. [Storage and WAL](../user-guide/storage-and-wal.md#the-write-ahead-log) has the details. A [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) has one log per shard, in the shard folders (`000/`, `001/`, …), so add those up for the total.


# Checking for damage

[check()](../reference/Cache.check.md#diskcache_oxide.Cache.check) compares the database with the value files and returns what it found as a list of `warnings.WarningMessage`, without issuing any warnings. An empty list means nothing is wrong. Here a value file is deleted behind the cache's back:


``` python
import glob

damaged = Cache(tempfile.mkdtemp())
damaged.set('report', b'x' * 100_000)   # 64 KiB or more: stored in a file of its own
damaged.set('summary', 'small')
[path] = glob.glob(os.path.join(damaged.directory, '*', '*.val'))
os.remove(path)                          # what a bad disk or a stray rm would do

for warning in damaged.check():
    print(warning.category.__name__, str(warning.message).replace(damaged.directory, '...'))
print('repaired:', len(damaged.check(fix=True)), '| after:', damaged.check(), '| entries:', len(damaged))
```


    UserWarning file not found: .../3f/3f2c80dc91a19a168a31d6fa7cfd9355.val
    repaired: 1 | after: [] | entries: 1


It reports SQLite's `integrity_check` lines, value files that are missing or the wrong size, files nobody owns that are over an hour old, and stored totals that don't match the rows. `check(fix=True)` also repairs: it removes entries whose files are missing or cut short (they read as misses anyway), removes unowned files in the value folders, corrects the totals, then runs `VACUUM`, which shrinks the database file to what's in use, and checkpoints.

Run it from a scheduled job [one worker claims](run-once.md), never from a health probe. `integrity_check` reads the whole database and `VACUUM` rewrites it, and writers and forks wait for both. `VACUUM` also needs room for a temporary copy of the database in `SQLITE_TMPDIR`, `TMPDIR` or `/tmp`.

``` python
import logging

log = logging.getLogger('my_app')

def nightly_cache_check() -> None:
    problems = cache.check()
    for warning in problems:
        log.warning('cache check: %s', warning.message)
    if problems:
        cache.check(fix=True)
```


# Log lines to alert on

Where diskcache-oxide has been told not to raise, it logs on the `diskcache_oxide` logger instead, and that line is then the only sign anything happened. Make sure the logger reaches your log aggregator or Sentry:

- A [Caches](../reference/Caches.md#diskcache_oxide.Caches) cache that can't open logs at `ERROR` with the traceback, once per retry, and hands out a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) meanwhile. An open that only timed out logs at `WARNING`.
- `durable=True` logs swallowed failures at most once a minute per cache handle, with counts by operation: `WARNING` for a busy lock, `ERROR` for a full disk, an I/O error or a failed [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete).
- `on_decode_error='miss'` logs a warning for a stored value it couldn't read.

The exact messages, and what to do about each, are in [Errors and what to do about them](../user-guide/errors.md#log-lines-when-the-library-was-told-not-to-raise).


# Things to know

- **[stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats) turns counting on.** Its [enable](../reference/Caches.enable.md#diskcache_oxide.Caches.enable) argument defaults to True, so a health check that reads the counts also starts them. `stats(enable=False)` turns counting off and stops the counts where they are.
- **Counts reset on restart.** Graph the hit ratio, not the raw counts, or publish the counts as above.
- **`len()` includes expired entries** until [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire), or the cleanup a [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) does, removes them. A scheduled [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) keeps it honest; `caches.expire_all()` does every registered cache.
- **Health checks shouldn't write.** Everything in `cache_health` is a read. [check()](../reference/Cache.check.md#diskcache_oxide.Cache.check) and [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) belong in a maintenance job.
