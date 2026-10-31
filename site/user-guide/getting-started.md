# Getting started

A [Cache](../reference/Cache.md#diskcache_oxide.Cache) is a directory. Open the same directory from any number of threads and processes and they all share one cache, which is still there after a restart. This page covers installing diskcache-oxide, the everyday operations, and how to shut down cleanly.


# Install

diskcache-oxide isn't on PyPI yet. Until the first release, build it from source, which needs a Rust toolchain:

``` bash
uv pip install git+https://github.com/mikeckennedy/diskcache-oxide
```

- **Python:** CPython 3.14 or 3.15, including the free-threaded builds (3.14t and 3.15t).
- **Platforms:** macOS and Linux. Windows support is planned.
- **Dependencies:** none at runtime. SQLite is bundled and compiled in, so your system's SQLite version doesn't matter ([diskcache_oxide.sqlite_version()](../reference/sqlite_version.md#diskcache_oxide.sqlite_version) reports the one in use).


# Open a cache

``` python
from diskcache_oxide import Cache

cache = Cache('/var/cache/my-app')
```

The directory is required. If it doesn't exist, diskcache-oxide creates it, readable and writable only by your user (`0700`), because anyone who can write to a cache directory can poison the cache.

Every process that should share the cache opens the same path. Inside one process, opening the same directory twice gives two handles on one shared engine, with one connection pool between them.


## Close it, or use `with`

``` python
with Cache('/var/cache/my-app') as cache:
    cache.set('greeting', 'hello')
```

[close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) returns the handle's SQLite connections. Once the last handle on a directory in a process closes, nothing in that directory is held open, so it's safe to move or delete.


# Store and read values

``` python
cache.set('user:42', {'name': 'Sam', 'roles': {'admin', 'editor'}}, expire=600)

user = cache.get('user:42')                 # the dict, with roles still a set
missing = cache.get('user:99', default={})  # {}

value, expires_at = cache.get('user:42', expire_time=True)
```

- Keys are `str`. Other key types raise `TypeError` for now (they're planned).
- Values can be `str`, `bytes`, `int`, `float`, `bool`, `None`, and lists, tuples, dicts, sets and datetimes made of those. They come back as the types you stored. See [Serialization](serialization.md).
- [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) is in seconds. Leave it out for an entry that never expires. `touch(key, expire=...)` changes the expiry of an existing entry.


## Dictionary syntax

``` python
cache['alpha'] = 100
assert 'alpha' in cache
assert cache['alpha'] == 100
del cache['alpha']        # KeyError if it's missing, like a dict
```


## Remove values

``` python
cache.delete('user:42')             # True if it was there
value = cache.pop('session:7')      # the value, or the default if it was missing
```


# Atomic operations across processes

These are what make a shared cache useful for coordination, not only for caching. Each one is a single SQLite transaction, so it's atomic across every thread and process using the directory.

``` python
# Store only if the key is absent (or expired). Exactly one caller wins.
if cache.add('job:refresh-feeds', os.getpid(), expire=3600):
    refresh_feeds()

# Counters. incr() keeps the entry's existing expiry.
cache.incr('page_views')          # 1
cache.incr('page_views', 5)       # 6
cache.decr('page_views', 2)       # 4
```

[incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) on a missing key starts from `default` (0). Pass `default=None` to get a `KeyError` instead. A key holding a non-number raises `TypeError`.


# List keys

``` python
for key in cache.iterkeys(prefix='user:'):
    print(key)
```

[iterkeys()](../reference/Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys) yields keys in sorted order (`reverse=True` for descending), and with `prefix=` it's an indexed range scan: 400,000 keys scan in about 10 ms, against about 25 seconds for diskcache. Iterating the cache itself (`for key in cache`) yields keys in insertion order.


# Size limits and eviction

``` python
cache = Cache(
    '/var/cache/my-app',
    size_limit=500 * 1024 * 1024,   # 500 MiB (default 1 GiB, minimum 1 MiB)
    cull_limit=10,                  # entries evicted per set() when over the limit (default 10)
)
```

When the cache's on-disk volume goes over [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit), each [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) evicts up to [cull_limit](../reference/Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit) of the least recently stored entries. `cache.volume()` reports the current size in bytes.

Maintenance calls, all safe to run while other processes use the cache:

- `cache.expire()` removes every expired entry and returns how many.
- `cache.clear()` removes every entry that existed when it started, and keeps anything written while it runs.
- `cache.cull()` evicts down to the size limit.


# Timeouts

When another process holds SQLite's write lock, a write waits for it, up to the cache's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) (60 seconds by default), then raises [diskcache_oxide.Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), a subclass of the built-in `TimeoutError`:

``` python
from diskcache_oxide import Cache, Timeout

cache = Cache('/var/cache/my-app', timeout=1.0)
try:
    cache.set('key', 'value')
except Timeout:
    ...  # the cache was too busy; carry on without it
```

Maintenance calls work in chunks, so they take the write lock many times. A request-sized timeout can cut an admin [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) off partway through, so [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) and [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) each take their own `timeout=` for that call. It bounds each chunk's wait for the lock, not the whole call:

``` python
cache = Cache('/var/cache/my-app', timeout=1.0)  # for requests
cache.clear(timeout=10.0)                        # for the admin page
```


# Values that can't be read

A cached value can stop being readable: a class you pickled was moved or renamed, or the bytes were damaged. By default [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) raises, as diskcache does. A cache is only a copy of data you can rebuild, so most apps would rather treat that as a miss. `on_decode_error` does that:

``` python
cache = Cache('/var/cache/my-app', pickle=True, on_decode_error='miss')
cache.get('homepage')  # None if the stored value can't be decoded; the bad row is deleted
```

- `'miss'` returns the default, deletes the bad row, and logs a warning on the `diskcache_oxide` logger. Only that exact stored version is deleted, so a fresh value another process wrote in the meantime survives, and the read never waits for the write lock to do it. If the lock is busy, the row stays and a later read tries again.
- A value this version can't read but another might, such as one written by a newer diskcache-oxide or one that needs a time zone this host lacks, is a miss too, but its row is kept. Reading it with the default `'raise'` raises [ForeignValueError](../reference/ForeignValueError.md#diskcache_oxide.ForeignValueError), a subclass of [CorruptValueError](../reference/CorruptValueError.md#diskcache_oxide.CorruptValueError).
- Pass a function instead to report the failure yourself, for example to Sentry. It's called as `report(key, exc)` in place of the warning, after the row has been dealt with. Anything it raises is logged, and the read is still a miss.
- Errors that aren't about the stored bytes still raise: [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), `MemoryError`, an I/O error, and [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) (a pickled value read by a cache without `pickle=True`, which is a configuration mistake, not damage).

It covers [get](../reference/Cache.get.md#diskcache_oxide.Cache.get), `cache[key]` (which raises `KeyError`) and [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop). A row that stays (a foreign value, or a bad one whose delete had to be skipped) fails on every read, so each stored version is reported once per cache handle, not on every read.

[pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) decodes the value before it deletes the row, so a value that another version or host might read survives it. This table is the usual path; the fallback described below it removes the row first:

| What went wrong | `'raise'` | `'miss'` or a function |
|----|----|----|
| A foreign value | raises [ForeignValueError](../reference/ForeignValueError.md#diskcache_oxide.ForeignValueError), row kept | returns the default and reports, row kept |
| [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) | raises, row kept | raises, row kept |
| An unreadable value (malformed bytes, a failing unpickle) | deletes that version, then raises | deletes that version, returns the default and reports |
| Any other error from a pickle hook ([Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), `MemoryError`, an I/O error) | raises, row kept | raises, row kept |

If that delete itself fails, for example with [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), its error is raised under every policy with the decode error as its `__cause__`, and the row stays.

A value still goes to exactly one caller, because [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) deletes only the version it decoded. A [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) that loses that delete to another writer reads again, and after 8 losses it falls back to removing the row first and decoding afterwards, so it can't starve. If its [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) has run out by the time it loses, it raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) instead: this [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) deletes nothing, and whatever the other writer left stays. There are two limits:

- That fallback is the one place a failed decode can cost the value, because the row is already gone: a foreign value in a known mode, or any error from a pickle hook ([Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), `MemoryError`, an I/O error). It's possible on a heavily overwritten key, since a [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) can lose while it queues for the write lock. How often it happens hasn't been measured yet.
- Like [get](../reference/Cache.get.md#diskcache_oxide.Cache.get), [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) doesn't wait to clean up a row whose large value file has gone missing. It returns the default, and if the write lock is busy the row can stay until a later [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) or write removes it.


# Shutting down

Call `cache.checkpoint()` at shutdown if you can. It isn't required. With readers in other processes, a burst of writes (a big fill, or a [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear)) can leave SQLite's write-ahead log much larger than the database, and SQLite only shrinks it when it gets a moment with no readers. [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) makes that moment: it waits briefly for in-flight reads, copies the log into the database, and truncates it. It returns `False` instead of raising if other processes keep it busy.

[close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) does the same by default, but only when the log is large, and it waits at most a second, so closing stays cheap. [Storage and WAL](storage-and-wal.md) has the details.


# Testing code that uses a cache

Two tools help here:

- **[NullCache](../reference/NullCache.md#diskcache_oxide.NullCache)** is a cache that's switched off: every write succeeds and every read misses. Swap it in for tests or local development without touching call sites. See [NullCache](null-cache.md).
- **`clock=`** controls the cache's clock. diskcache-oxide reads time natively in Rust, so patching `time.time` doesn't affect it on its own. Pass `clock=lambda: time.time()` and the cache follows your patched clock:

``` python
cache = Cache(tmp_path, clock=lambda: time.time())
```
