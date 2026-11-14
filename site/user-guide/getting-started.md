# Getting started

A [Cache](../reference/Cache.md#diskcache_oxide.Cache) is a directory. That's the whole mental model, and it carries further than it looks: open the same directory from any number of threads and processes and they all share one cache, nobody has to be told about anybody else, and the entries are still there after a restart, because a directory doesn't restart. Hold on to that sentence; most of what follows is a consequence of it. This page covers installing diskcache-oxide, the everyday operations, the handful of things that only start to matter once several processes are involved, and how to shut down cleanly.


# Install

``` bash
pip install diskcache-oxide
```

- **Python:** CPython 3.14 or 3.15, including the free-threaded builds (3.14t and 3.15t).
- **Platforms:** macOS, Linux and Windows. Wheels cover macOS on Apple Silicon, Linux (glibc) on x86_64 and aarch64, and Windows on x86_64; anywhere else pip builds from source, which needs the latest stable Rust toolchain. [Windows](#windows) below says what differs there.
- **Dependencies:** none at runtime. SQLite is bundled and compiled in, so your system's SQLite version doesn't matter ([diskcache_oxide.sqlite_version()](../reference/sqlite_version.md#diskcache_oxide.sqlite_version) reports the one in use).
- **Upgrades:** a release that keeps the on-disk format opens your caches as they were, warm. One that changes it (the changelog says so first) opens a new cache in a subdirectory named for its format and logs a warning, so nothing fails at deploy; [Upgrading across a format change](../recipes/upgrading.md) covers cleaning up afterwards.


# Open a cache

``` python
from diskcache_oxide import Cache

cache = Cache('cache')  # a folder named "cache" in the current directory
```

The directory is required. If it doesn't exist, diskcache-oxide creates it, readable and writable only by your user (`0700`). That's deliberate: anyone who can write to a cache directory gets to decide what your app reads next, so the directory starts out as private as your home directory.

So a cron job, an admin script or a container running as a different user can't open a directory your app created: it gets `PermissionError` ("Permission denied"). Run everything that shares a cache as the same user. Loosening the directory's mode yourself doesn't help, since the folders diskcache-oxide creates inside it for large values are owner-only too.

Every process that should share the cache opens the same path. Inside one process, opening the same directory twice gives two handles on one shared engine, with one connection pool between them.

On your laptop any folder you own will do. In production, give the app a directory it owns on a local disk, such as `/var/lib/my-app/cache/<name>` (systemd's `StateDirectory=my-app` creates `/var/lib/my-app` for the service's user, and `/var/cache` is often root-owned). On Windows, use a folder under `%LOCALAPPDATA%` for a per-user app. [Sharing a cache across web workers](../recipes/web-workers.md#the-recommended-setup) has the layout we recommend for an app with several caches, and [Docker and Compose](../recipes/docker.md) the container version.


## Close it, or use `with`

``` python
with Cache('cache') as cache:
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

- Keys are `str`, `bytes`, `int`, `float`, `bool`, `None`, or tuples of those, such as `('user', 42)`. They're compared by exact type, so `1`, `1.0` and `True` are three keys. Anything else (a `list`, a `dict`, an object) raises `TypeError`; build a tuple or a string instead.
- Values can be `str`, `bytes`, `int`, `float`, `bool`, `None`, `Decimal`, and lists, tuples, dicts, sets, frozensets, datetimes and dates made of those. They come back as the types you stored. See [Serialization](serialization.md).
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


## Cache a function's results

[memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) wraps the check-the-cache, compute-on-a-miss, store-the-result pattern in a decorator, keyed on the function's name and its arguments:

``` python
@cache.memoize(expire=600)
def episode_details(episode_id: int) -> dict:
    return db.load_episode(episode_id)
```

It's diskcache's [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize), with the same keys. The arguments become part of the key, so they must be key types. [Caching expensive calls](../recipes/caching-expensive-calls.md) covers it in full.


# Atomic operations across processes

Picture eight gunicorn workers, each with a scheduler that fires the same hourly job. You want it to run once. There's no leader, no Redis, and no way for the workers to talk to each other except through the cache. That turns out to be enough. Each call below is a single SQLite transaction, so it's atomic across every thread and process using the directory, and "store only if absent", done atomically, is a lock if you squint. These are what make a shared cache useful for coordination, not only for caching.

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

To make several calls atomic together, group them with `with cache.transact() as tx:`. See [Transactions](transactions.md).

The same atomic calls are the building blocks for more. [Coordination](coordination.md) has locks, semaphores and a throttle that hold across every process, and [Work queues](../recipes/work-queues.md) shows [push()](../reference/Cache.push.md#diskcache_oxide.Cache.push) and [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull), a queue that hands each item to exactly one worker.


# List keys

``` python
for key in cache.iterkeys(prefix='user:'):
    print(key)
```

[iterkeys()](../reference/Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys) yields keys in sorted order (`reverse=True` for descending), and with `prefix=` it's an indexed range scan: 400,000 keys scan in about 10 ms, against about 25 seconds for diskcache. Iterating the cache itself (`for key in cache`) yields keys in insertion order, and `reversed(cache)` newest first.


# Size limits and eviction

``` python
cache = Cache(
    '/var/cache/my-app',
    size_limit=500 * 1024 * 1024,   # 500 MiB (default 1 GiB, minimum 1 MiB)
    cull_limit=10,                  # entries evicted per set() when over the limit (default 10)
)
```

When the cache's on-disk volume goes over [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit), each [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) evicts up to [cull_limit](../reference/Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit) of the least recently stored entries. `cache.volume()` reports the current size in bytes. For a cache that must never drop a live entry (job claims, locks, a queue), pass `eviction_policy='none'`: nothing is evicted for size, expired entries are still removed, and the cache can grow past [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit). Like the limits, the policy is stored in the cache, so every process shares it.

Maintenance calls, all safe to run while other processes use the cache:

- `cache.expire()` removes every expired entry and returns how many.
- `cache.clear()` removes every entry that existed when it started, and keeps anything written while it runs.
- `cache.cull()` removes expired entries, then evicts down to the size limit (unless the policy is `'none'`), and returns how many it removed.
- `cache.evict(tag)` removes every entry stored with `set(..., tag=...)`, so a group of entries under unrelated keys can be dropped at once (see [Invalidating groups of keys](../recipes/invalidating-groups.md#option-2-tags)).
- `cache.check()` compares the database with the large-value files and reports what doesn't match; `check(fix=True)` repairs it.

`cache.reset('size_limit', 2 * 2**30)` stores a new value for one of the settings kept in the cache ([size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit), [cull_limit](../reference/Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit) or [eviction_policy](../reference/Cache.eviction_policy.md#diskcache_oxide.Cache.eviction_policy)), and `cache.reset('size_limit')` reads the stored one. `cache.stats()` turns on hit and miss counting for this process, and later calls return `(hits, misses)`. The counts live in memory, so other processes' lookups aren't included and counting never makes a read write.


# Timeouts

One process at a time commits to a database file. When another one is in the middle of doing so, your write waits its turn, up to the cache's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) (60 seconds by default), then gives up and raises [diskcache_oxide.Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), a subclass of the built-in `TimeoutError`:

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

What else [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) can mean, and what to do about every other exception the library raises, is in [Errors and what to do about them](errors.md).


# Values that can't be read

A cache outlives the code that filled it. Deploy a rename of a class you pickled, or have a disk hand back damaged bytes, and a row that read fine yesterday raises today. By default [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) raises, as diskcache does. But a cache is only a copy of data you can rebuild, so most apps would rather treat that row as a miss and move on. `on_decode_error` does that:

``` python
cache = Cache('cache', pickle=True, on_decode_error='miss')
cache.get('homepage')  # None if the stored value can't be decoded; the bad row is deleted
```

- `'miss'` returns the default, deletes the bad row, and logs a warning on the `diskcache_oxide` logger. Only that exact stored version is deleted, so a fresh value another process wrote in the meantime survives, and the read never waits for the write lock to do it. If the lock is busy, the row stays and a later read tries again.
- A value this version can't read but another might, such as one written by a newer diskcache-oxide or one that needs a time zone this host lacks, is a miss too, but its row is kept. Reading it with the default `'raise'` raises [ForeignValueError](../reference/ForeignValueError.md#diskcache_oxide.ForeignValueError), a subclass of [CorruptValueError](../reference/CorruptValueError.md#diskcache_oxide.CorruptValueError).
- Pass a function instead to report the failure yourself, for example to Sentry. It's called as `report(key, exc)` in place of the warning, after the row has been dealt with. Anything it raises is logged, and the read is still a miss.
- Errors that aren't about the stored bytes still raise: [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), `MemoryError`, an I/O error, and [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) (a pickled value read by a cache without `pickle=True`, which is a configuration mistake, not damage). [`durable=True`](#a-cache-that-cant-take-a-request-down) makes a [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), an I/O error or a `RecursionError` from a pickle hook a miss too.

It covers [get](../reference/Cache.get.md#diskcache_oxide.Cache.get), `cache[key]` (which raises `KeyError`) and [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop). A row that stays (a foreign value, or a bad one whose delete had to be skipped) fails on every read, so each stored version is reported once per cache handle, not on every read.

[pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) decodes the value before it deletes the row, so a value another version or host might read survives it. What each policy does with each kind of failure, for [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) and [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop), is in [Reading damaged values](errors.md#reading-damaged-values).


# A cache that can't take a request down

It's 3 a.m., the disk under the cache is full, and every request that writes to the cache is now a 500. Nothing the cache held was precious: it was all a copy of something the database still has. If every caller can fall back to its source of truth, an outage in the cache should cost you speed, not requests. `durable=True` makes that the rule for the calls a request makes:

``` python
cache = Cache('cache', timeout=1.0, durable=True)
page = cache.get('homepage')  # None if the cache is locked, full, broken or the disk failed
```

"Durable" here means the cache keeps your app running. It doesn't mean what it means in a database, where a durable write survives a crash. This option does the opposite: it can drop writes. It's off by default.

An operational failure is a [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), an `OSError` (disk full, I/O), a SQLite error including a damaged database, or a child forked inside a transaction that can't write. On a single-key call it's answered like this:

| Call | On an operational failure |
|----|----|
| [get](../reference/Cache.get.md#diskcache_oxide.Cache.get), [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) | the default; `get(key, expire_time=True)` gives `(default, None)` |
| `key in cache` | `False` |
| `cache[key]` | raises `KeyError` |
| [set](../reference/Cache.set.md#diskcache_oxide.Cache.set), [add](../reference/Cache.add.md#diskcache_oxide.Cache.add), [touch](../reference/Cache.touch.md#diskcache_oxide.Cache.touch) | `False` |
| `cache[key] = value` | drops the write |
| [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr), [decr](../reference/Cache.decr.md#diskcache_oxide.Cache.decr) | `default + delta` or `default - delta`, as [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) does. `default=None` raises `KeyError` |
| [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete) | `False` |
| `del cache[key]` | raises `KeyError` |

These always raise:

- **Bugs.** A key of a type that can't be a key, a value that can't be stored without pickle, a non-numeric [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr), an [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) past 64 bits, [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError), and misusing a transaction.
- **`MemoryError` and `KeyboardInterrupt`.** That includes SQLite running out of memory.
- **Admin calls.** [clear](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire), [cull](../reference/Cache.cull.md#diskcache_oxide.Cache.cull), [checkpoint](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint), `len`, [volume](../reference/Cache.volume.md#diskcache_oxide.Cache.volume) and iteration, because their callers need the truth, and most have their own `timeout=`. [checkpoint](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) still returns `False` when it's only busy.
- **The constructor, and starting a [transaction](transactions.md).** Use [`Caches`](caches.md) if opening shouldn't raise: it gives you a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) until the cache can open.
- **Anything inside a transaction you hold on this cache.** A dropped write there would break the transaction's atomicity.
- **Errors from your own code**, other than the pickle hooks on a read described above. A `clock=` function that raises, or a pickle hook on a write, raises as usual, even if what it raises is a [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout).

`on_decode_error` defaults to `'miss'` under `durable=True`, and a value you pass wins.


## What a dropped call costs

A failure reaches you as "wasn't there", so know what each one means before you turn this on.

- **A dropped [set](../reference/Cache.set.md#diskcache_oxide.Cache.set), [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete), [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) or version-bump [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) serves stale data.** The old entry stays. It's served until it expires, and with no expiry that means forever, or until a later write replaces it. [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete) returns `False`, the same as for a key that wasn't there, so your code can't tell. The log line is the only signal. If stale data would be a real problem, give those entries an expiry.
- **A [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) whose delete fails returns the default, not the value**, since the row is still there and a later [pop](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) could deliver the value a second time.
- **A fake [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) isn't a count.** It's what a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) returns, so a rate limiter fails open. Don't use one as an id: two callers can get the same number.
- **[add](../reference/Cache.add.md#diskcache_oxide.Cache.add) fails closed.** It returns `False` under `durable=True`, but `True` on a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache), so [Caches](../reference/Caches.md#diskcache_oxide.Caches) can give you either answer for one key. Use a separate handle without [durable](../reference/Cache.durable.md#diskcache_oxide.Cache.durable) for claims and locks, and treat its exceptions as "not claimed".


## It doesn't bound time

A call still waits up to its [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) before it gives up, so pair `durable=True` with a small `timeout=`. Not too small: [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) still has to exceed the work in flight, plus a fork, plus the interpreter handing over, or ordinary forks turn into misses. Beside busy threads on a GIL build, that hand-over alone takes tens to hundreds of milliseconds. See [How long a call waits for a fork](concurrency-and-forks.md#how-long-a-call-waits-for-a-fork).


## Reporting

Every swallowed failure is counted in one of three groups, and logged on the `diskcache_oxide` logger, so one group can't hide another:

| Group | Level | What's in it |
|----|----|----|
| Busy | `WARNING` | [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout): a lock or a fork outlasted [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) |
| Failing | `ERROR` | Everything else: a full disk, an I/O error, a damaged database |
| Lost invalidations | `ERROR` | Any failed [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete) |

Each group logs its first failure at once, then at most once a minute per cache handle, with counts by operation since its last line: `get: 1204, set: 37`. An outage can be thousands of failures a second, and that's one line a minute. Failures after a group's last line stay counted until its next one, or until you call [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close), which logs what's left. Dropping a handle without [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) doesn't. An outage that ends within the minute can therefore show only its first failure until then, which is why each line says more may follow. A hook outage on a read is reported once per stored version, as in the table in [Reading damaged values](errors.md#reading-damaged-values), and counted here as busy for a timeout and failing otherwise, with no line of its own: the group's next line includes it. Time comes from the failing call's clock, so `clock=` drives it in tests. `cache._durable_stats()` has the running totals by group and operation, the place to look for "how many invalidations did we lose?". It's a private hook for now, like `_wal_stats()`.

The limit is per handle, so a `with Cache(...)` per request logs on every request during an outage. A handle from [Caches](../reference/Caches.md#diskcache_oxide.Caches) is opened once, so it isn't affected.


# More than one process writing

A single [Cache](../reference/Cache.md#diskcache_oxide.Cache) has one write lock per directory. Reads never wait for it, but every write does, so eight worker processes that all bump counters are eight writers lining up for one lock. Start with plain caches anyway: most apps mostly read, a plain cache uses less memory (one connection pool, not one per shard), and its writes take about 20 µs. If one cache that many processes write to (counters, rate limits) does show the lines, open it as a [`FanoutCache`](fanoutcache.md): it spreads the keys over several databases, each with its own lock, and in the repository's benchmark it holds its rate at 8 processes where a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) falls to about 40% of its one-process rate.


# Shutting down

Call `cache.checkpoint()` at shutdown if you can. It isn't required. With readers in other processes, a burst of writes (a big fill, or a [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear)) can leave SQLite's write-ahead log much larger than the database, and SQLite only shrinks it when it gets a moment with no readers. [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) makes that moment: it waits briefly for in-flight reads, copies the log into the database, and truncates it. It returns `False` instead of raising if other processes keep it busy.

[close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) does the same by default, but only when the log is large, and it waits at most a second, so closing stays cheap. [Storage and WAL](storage-and-wal.md) has the details.


# Windows

The API and the guarantees are the same on Windows, with these differences:

- **No `fork()`.** Windows has no fork, so `multiprocessing` starts each worker fresh and each opens its own handle. The [fork guarantees](concurrency-and-forks.md) are Unix-only; threads and processes are safe everywhere.
- **An open cache's directory can't be deleted.** Windows won't delete a file a process has open, and SQLite keeps the database open for as long as any handle is. [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) every handle first: a closed cache's directory deletes cleanly. A delete attempted while it's open fails with `PermissionError`, and may still remove some large values' files on the way, which then read as misses.
- **New directories take their parent's permissions.** On macOS and Linux a cache directory is created owner-only (0700); on Windows it inherits its parent's access control list, so where you put it decides who can read and write it. For an app run by one user, a folder under `%LOCALAPPDATA%` is private to that user: `platformdirs.user_cache_dir('my-app', 'my-company')` gives `%LOCALAPPDATA%\my-company\my-app\Cache`. Avoid `C:\ProgramData`, where by default any user can create files, and so could change what your app reads. For a directory elsewhere, remove inherited access and grant only the account the app runs as: `icacls D:\caches\my-app /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"` in Command Prompt, or `"$env:USERNAME:(OI)(CI)F"` in PowerShell.
- **Other programs holding a file.** Antivirus, the search indexer and backup tools can hold a large value's file for a moment. A read or a delete waits that out for about 50 ms. A read still blocked after that raises (or, on a `durable=True` cache, is a miss, and the entry stays); a file a delete couldn't remove is left on disk with no entry, outside [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit), until `check(fix=True)` removes it (it removes a file no entry names once the file is an hour old). Excluding cache directories from real-time antivirus scanning avoids this and can make them faster (with Microsoft Defender, `Add-MpPreference -ExclusionPath <directory>` in an administrator PowerShell). It's a speed option, not a fix: our Windows stress tests pass with Defender scanning, and a cache can hold bytes fetched from elsewhere, so weigh what the exclusion lets through.
- **Long paths work.** A cache directory can be deeper than Windows's 260-character limit, whether or not the machine enables long paths.


# Testing code that uses a cache

Two tools help here:

- **[NullCache](../reference/NullCache.md#diskcache_oxide.NullCache)** is a cache that's switched off: every write succeeds and every read misses. Swap it in for tests or local development without touching call sites. See [NullCache](null-cache.md).
- **`clock=`** controls the cache's clock. diskcache-oxide reads time natively in Rust, so patching `time.time` doesn't affect it on its own. Pass `clock=lambda: time.time()` and the cache follows your patched clock:

``` python
cache = Cache(tmp_path, clock=lambda: time.time())
```
