# Changelog

All notable changes to diskcache-oxide are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions are MAJOR.MINOR.BUILD, which isn't semantic versioning: MINOR goes up for new features and behavior changes, MAJOR for breaking ones, and BUILD counts releases and never resets. During 0.x, a release that changes the on-disk format bumps the minor version and says so first in its entry. Since 0.3.5, a cache in another format opens a new cache in a versioned subdirectory instead of failing, and `remove_old_formats()` cleans up the old one (see [Upgrading across a format change](https://mkennedy.codes/docs/diskcache-oxide/recipes/upgrading.html)). The last number goes up with every release, a minor bump included, so 0.1.2 is followed by 0.2.3.

## [Unreleased]

No format change: caches made by 0.3.5 and later open as they are. One behavior change, for a child process forked at the wrong moment, and fixes from the same review as 0.4.7 that touch the engine's core.

### Changed

- **A child forked inside a transaction, or from inside a cache call that has its connection out, can't use that cache at all.** Every call on it in that child raises `RuntimeError` at once, reads included, for the life of the process, on a `durable=True` handle too; `close()` still works, other caches are unaffected, and on a `FanoutCache` only the shard involved is refused. Before, a child forked inside `with cache.transact():` could still read, and one forked from a pickle hook or a `decode` function reading a small value inside a call could read and write. Both were unsafe: such a child holds none of the file locks SQLite believes it holds, so once the parent closed, its reads went stale and its `set` calls returned `True` and were lost, with no error and nothing for `check()` to find. Fork before or after the block, or start workers with `spawn` or `forkserver` (D-020; see [Concurrency and forks](https://mkennedy.codes/docs/diskcache-oxide/user-guide/concurrency-and-forks.html#forking-from-inside-your-own-transaction)). The child logs one warning on the `diskcache_oxide` logger naming the directories.
- `incr` and `decr` raise `ValueError` for a `delta` or `default` that is an infinity or a NaN (D-064). A counter that reached NaN was stored with no value, so `get` raised `CorruptValueError` and the next `incr` raised `TypeError`. A counter left that way by an earlier version stays unreadable until it's overwritten or deleted.
- The error `this database can't be written in a process forked inside a transaction` is gone: that child now gets the error above for every call.

### Fixed

- Another library's before-fork hook that used a cache (one registered before `diskcache_oxide` was imported, so it runs after ours) left its connection open across the fork, and the child then lost its writes once the parent closed. A connection given back between the fork hooks is closed now, and that child works.
- A child forked while another thread was leaving the forking thread's transaction (`__exit__` called from a second thread) kept a lock level nothing could release: `os.fork()` from another thread of that child hung, and leaving the block there waited out the timeout. It raises `RuntimeError` at once now, and later forks go ahead.
- One row with a damaged `filename` or `size` column (the wrong SQLite type, as a bad page or an edit by hand leaves it) made every `get`, `delete`, `clear()` and `check(fix=True)` that reached it raise `OSError: Invalid column type`, and once it was the oldest row, every `set` that had to evict. A row with a damaged `filename`, or a damaged `size` on a value stored in a file, reads as a miss and is removed like any other, and `check()` reports a row with a wrong-typed `mode`, `size`, `store_seq`, `expire_time` or `filename` (`damaged row N: its COLUMN is stored as TYPE`), which `fix=True` removes.
- `check(fix=True)` could remove the value file of a transaction held open for over an hour in another process, which then committed an entry with no file. It removes a file no entry names only under the write lock now, so it waits for that transaction (and raises `Timeout` if it outlasts the timeout).
- With a `types=` registry on a handle, a value of a class registered only in the global registry was stored under the global name and read back through the handle's own registration of that name, as another class. `set` raises `TypeError` for it now.
- Background maintenance's checkpoint read "another connection is checkpointing" as "everything is copied" and went on to reset the write-ahead log under the write lock, where the whole copy could then run and hold up writers.
- `check(fix=True)` could wait about twice its `timeout`: the `VACUUM` at its end and the checkpoint after it each got the full time.

## [0.4.7] - 2026-11-19

No format change: caches made by 0.3.5 and later open as they are. Fixes from a bug, race and security review of 0.4.6.

### Fixed

- `DjangoCache.delete_many` under `durable=True` no longer stops at a missing key once the batch has run for half a lock wait, which left every key after it undeleted on a healthy cache (with the default 1.0 s lock wait, any missing key after the first half second of a long batch; with a lock wait of 0, the first missing key). `set_many` had the same flaw for a key that failed at once: it and every key after it were returned as not stored without being tried. Both now stop only at a call that itself waited half a lock wait, which is what a held lock looks like, and with a lock wait of 0 they try every key.
- `pull()` on a `timeout=0` handle with `on_decode_error='miss'` raised `Timeout` after removing an unreadable item from the front of the queue, although nothing held the lock, and so did any handle whose timeout ran out just as the item was removed. It now goes on to the next item, as documented; only a puller that loses a race with no time left raises `Timeout`.
- `check(fix=True)` removed the row of a value whose file it couldn't look at, as if the file were missing: a value folder without permission, or any other error from the file system, cost a whole entry and left its file behind as an orphan. Only a file that isn't there counts as missing now, as for a read; any other error ends the check with that `OSError`, and nothing is removed.
- `remove_old_formats()` on an old `FanoutCache` root removed a shard's database when it couldn't tell its format: an empty file (which a process creating the shard holds for a moment), a database with no format stamp, or a file that isn't a database. It now leaves and reports them, as it always has for a plain cache, and as its documentation said.
- With `serializer='json'`, a dict with non-`str` keys nested 43 or more deep (each inside the last) was stored by `set` and then couldn't be read back: `get` raised `CorruptValueError`, or with `on_decode_error='miss'` deleted it as a miss. `set` now raises `TypeError` for it, as for any value nested too deep, from 41 levels. msgpack was never affected.

## [0.4.6] - 2026-11-19

No format change: caches made by 0.3.5 open as they are.

**A Django cache backend.** `'BACKEND': 'diskcache_oxide.DjangoCache'` runs Django's cache framework on diskcache-oxide: the low-level API, the per-view and per-site cache, template fragments and the cache session engines, with every worker process sharing one cache on disk and `add()` and `incr()` atomic across processes. It passes Django's own cache tests (`BaseCacheTests`, the middleware, template-response and i18n tests, and the cache and cached_db session tests) from the source of Django 5.2, 6.0 and 6.1, on Python 3.14 and 3.14t, and 6.1 on 3.15 and 3.15t. `OPTIONS` takes `Cache`'s keywords by their own names, and `shards` makes the alias a `FanoutCache`. Its defaults differ from `Cache`'s where Django needs them to: a 1.0 s lock wait, and `pickle=True`, since Django caches responses, `SafeString`s and model instances. Errors raise unless `OPTIONS['durable']` is set; a durable alias for the cache or cached_db session engine is refused. See the new Django cache backend page.

### Added

- `diskcache_oxide.DjangoCache`, loaded on first use, so `import diskcache_oxide` still never imports Django. Beyond Django's API it has `tag=` on `set` and `add`, `evict(tag)`, `pop`, `expire()`, `cull()`, `stats()`, `maintenance_stats()`, `check()` and `cache`, the `Cache` underneath; `diskcache_oxide.djangocache.close_all()` releases every handle it opened (it runs by itself when `override_settings` changes `CACHES`).
- The `django` extra, `pip install 'diskcache-oxide[django]'`, which asks for Django 5.2 or newer.
- `examples/django/`, a Django project on two aliases (pages durable, sessions and counters plain) under gunicorn, with its tests.

### Fixed

- A value holding a lone surrogate inside it, such as `{'q': '\ud800'}` (one can arrive in a JSON body and end up in a session), is now pickled under `pickle=True`, as diskcache stores it, instead of raising `UnicodeEncodeError` at `set`. Without pickle it still raises, and a lone surrogate in a top-level `str` still raises either way, as it does in diskcache. Values the serializer could already store are written exactly as before.

### Documentation

- An "Example apps" section: walkthroughs of the FastAPI example (`examples/webapp`) and the Django example (`examples/django`), file by file.
- The Django page has measured numbers: reads, writes, worker processes and threads against diskcache's backend and Django's own `FileBasedCache`, `DatabaseCache` and `LocMemCache` (`bench/django.py`), and why `aget` hops to a thread though the read is faster than the hop.
- Corrected: a plain `Cache` slowing past two or three worker processes isn't the write lock. It happens with no writes at all (`bench/read_procs.py`: 836k ops/s at 2 processes, 296k at 8), most likely SQLite's shared-memory read lock. `FanoutCache` is now recommended for a cache several worker processes use heavily, reading or writing, not only one they write to; one worker process doesn't need it.
- A lone surrogate and an int past Python's 4,300-digit limit are documented: the first falls back to pickle inside a value, the second always raises (D-063).

## [0.3.5] - 2026-11-16

**Upgrades across a format change no longer fail.** A cache directory that holds a cache in another on-disk format (made by an older release, or by a newer one before a rollback) now opens a new, empty cache in its subdirectory `diskcache-oxide-format-N`, N being this release's format (3), instead of raising `FormatMismatchError`, and logs a warning on the `diskcache_oxide` logger once per process. Of the other format's cache, only its format is read, and its contents are never changed (if that read is the last connection to close on it, SQLite may checkpoint its write-ahead log into the database file, which changes no data). Once the subdirectory exists, every later open uses it. A `FanoutCache` moves as a whole. This release keeps format 3, so caches made by 0.2.3 open where they are, warm; a cache still at format 2 (0.1.2) opens `diskcache-oxide-format-3` beside it. See "Upgrading across a format change" in the recipes.

**Caches look after themselves.** One small thread per process now does each open cache's housekeeping in the background, every 5 minutes by default: it removes expired entries and their files, trims a cache left over `size_limit` (`cull_limit` entries at a time; never under `eviction_policy='none'` or with `cull_limit=0`), checkpoints a write-ahead log grown past half of `wal_size_limit`, and hourly clears temporary files a killed writer left and empty value folders, `FanoutCache` shards included. There's no longer an `expire()` to schedule. The caches in a process take turns across the interval, and the processes sharing a cache take turns through two new rows in its `settings` table (no format change: older releases ignore them). It never waits for a lock and holds the write lock only for short chunks, so requests don't notice it, and it stops for every `os.fork()` and starts again after, so Python never warns about forking a multi-threaded process. A cache opened with `clock=` gets none. Pass `maintenance_interval=0`, on every handle the process opens on it, for a cache this process must never write, such as a read-only replica: even a tidy cache is written once an hour to coordinate the processes (D-061).

### Added

- `maintenance_interval=` on `Cache`, `FanoutCache`, `Caches` and `Caches.register`: seconds between background maintenance turns for the cache in this process (default 300, 0 for off, 1 second to a day). Per process, not stored in the cache; the shortest any open handle asked for wins.
- `maintenance_stats()` on `Cache`, `FanoutCache` and `NullCache`, returning a `MaintenanceStats` dict (`interval`, `turns`, `removed`, `failures`, `last_error`); `Caches.stats()` carries it as `CacheStats.maintenance`. A failure is also logged once per streak, as a WARNING on the `diskcache_oxide` logger, by the next single-key call on the cache.

- `Caches.get(name, default=None)`: the named plain cache as `caches[name]` returns it, or `default` when no cache by that name is registered, so a check and a lookup are one call (`if (pages := caches.get('pages')) is None: ...`). A registered cache that can't open or is disabled still comes back as a `NullCache`.

- `remove_old_formats()` on `Cache`, `FanoutCache` and `NullCache` (where it returns `[]`), and on `Caches` for every registered cache: removes caches in an older format from the cache's directory, only files diskcache-oxide wrote, never following links, never a newer or the current format, and returns what it removed. It doesn't check whether an older worker still uses those files: call it once they've all stopped.

- Windows on x86_64, with wheels for CPython 3.14, 3.14t, 3.15 and 3.15t. The API and the on-disk format are the same; caches made on macOS or Linux aren't affected. Windows has no `fork()`, so the fork guarantees are Unix-only. Getting started has a new Windows section: an open cache's directory can't be deleted, new directories inherit their parent's permissions, and a value file another program holds past about 50 ms of retries is left until `check(fix=True)` removes it, an hour after it was written. SQLite uses its own Win32 mutexes there (`_sqlite_mutex_status()` reports `'native'`).

### Changed

- The `CacheLike` protocol gained `maintenance_stats()`, so a hand-written fake checked with `isinstance(x, CacheLike)` needs it too.
- An explicit `expire()` or `cull()` can return less than it did, since background maintenance may have removed the rows first, and `volume()` and the write-ahead log's size can change between calls.

- A cache directory holding another on-disk format opens the `diskcache-oxide-format-N` subdirectory with a warning instead of raising `FormatMismatchError` (D-059). `FormatMismatchError` is now raised only for another format inside that subdirectory. `directory` on `Cache` and `FanoutCache` reports where the cache really is, the subdirectory after such a move; a pickled handle keeps the directory as passed and resolves it again. A `Caches` registry no longer hands out a `NullCache` for a format change.
- A plain `Cache` opened on a `FanoutCache` shard directory in an older format raises `LayoutMismatchError`, as it always did for a current one, rather than `FormatMismatchError`.
- `FanoutCache(directory, shards=n)` on a root that records another count no longer raises: it opens with the stored count and logs a `WARNING` once per process. The new keyword `raise_on_mismatch=True` restores the `LayoutMismatchError`, and `Caches.register(..., raise_on_mismatch=True)` turns it into an open failure (a reported `NullCache`). A root in another on-disk format redirects to its versioned subdirectory either way. The `LayoutMismatchError` for settings that don't fit the stored count now says `shards=n was asked for` rather than `is registered with`, since a plain `FanoutCache` raises it too.
- `len()`, `iterkeys()`, `iter()` and `reversed()` leave out an entry the moment it expires, as `get` and `in` already did, on `Cache` and `FanoutCache` (D-060). Before, an expired entry was counted and listed until a `set()`, `expire()` or `cull()` removed its row, so a cache that's mostly read could report entries nobody could read. Nothing is written by the check; the rows and their space, in `volume()`, stay until something removes them: a `set()`, `expire()` or `cull()`, or background maintenance, which frees a mostly-read cache's space within its interval (D-061). `len()` now also counts the expired rows not yet removed, through the expiry index, so it costs a little more while such rows are waiting.

### Fixed

- `import diskcache_oxide` no longer calls `os.register_at_fork` where it doesn't exist.
- A value file that hits a full quota (`EDQUOT`) now raises `OSError` with `errno.ENOSPC`, as a full disk does, since a full disk is recognized by the error's kind rather than one Unix error number.
- `iter(cache)` (and a `FanoutCache`'s, shard by shard) could yield a key twice: one deleted and stored again while the walk ran got a newer row, which the walk met again at its end. It now stops at the newest row when it began, as diskcache's does, so a key stored meanwhile isn't yielded and a re-stored one is yielded once. Found by the 24-hour soak, 8.5 hours in.
- `iter(cache)` and `reversed(cache)` no longer sort the whole table for every page of keys. Without SQLite statistics, the planner read insertion order through the key index and sorted it, so walking a large cache took time in proportion to its size for every 512 keys (about 2 s for 200,000 entries on an M5 Max, now 22 ms); each page now seeks on the row id.

### Documentation

- An example app in `examples/webapp`: a FastAPI app with gunicorn, a background worker and Docker Compose, built the way the docs recommend, with tests that run it as real processes. Every page shows which cache reads served it.
- One recommended production setup throughout the docs: a durable `Caches` registry with `timeout=1.0` for data that can be rebuilt, and state (sessions, job claims, locks, queues) in plain caches opened directly, with eviction off, under one `/var/lib/my-app` directory. The Caches guide explains why state never goes through the registry.
- New pages: Docker and Compose (volumes, a fixed UID, ownership, Kubernetes, disk sizing, backups, cleanup after a format change) and Troubleshooting (start from the symptom).
- Corrected: the docs no longer claim network filesystems are detected at open. SQLite can't tell, so keep caches on a local disk.
- Also new: a keyword table and the state-copying loop in Switching from diskcache, Windows notes in Getting started, a Prometheus collector in Monitoring, a `dictConfig` that keeps the `diskcache_oxide` logger, and API reference sections for types, memoize, coordination and every error.

## [0.2.3] - 2026-11-12

**Delete your caches when you upgrade.** This release changes the on-disk format (format 3, for tags and the eviction policy). Opening a cache made by 0.1.2 raises `FormatMismatchError`, naming the database: stop every process using the cache, delete its directory (a FanoutCache's whole root, never one shard), and it's recreated empty on the next open. 0.1.2 refuses a format-3 cache the same way, and neither version writes anything into the other's. A 0.1.2 worker still running when its cache is deleted and recreated can't change the new one either: once it opens the new file, its writes, deletes and maintenance calls fail with "no such table" (only a `touch` gets through), because format 3 renames the table they all use.

### Added

- Keys of every native type: `bytes`, `int` (64-bit), `float`, `bool`, `None`, and tuples of those, nested up to 32 deep, beside `str`. They're never pickled and compare by exact type, so `1`, `1.0` and `True` are three keys. `iterkeys()` lists `str` keys first, then the other types in a fixed order; `iterkeys(prefix=...)` lists `str` keys only. A FanoutCache routes every key type. Type checkers see `diskcache_oxide.Key`.
- `memoize(name=None, typed=False, expire=None, tag=None, ignore=())` on `Cache`, `FanoutCache` and `NullCache`, as diskcache's, with the same keys, so `f.__cache_key__(...)` returns the tuple diskcache would. Every argument must be a key type, checked before the function runs; `typed=True` stores type names. `Memoized` and `Memoizer` type the wrapper.
- `CorruptKeyError`, a `CorruptValueError`, raised by iteration that meets a stored key it can't read (a damaged page or file); it never skips one.
- `reversed(cache)`, `pop(key, expire_time=True)`, a read-only `durable` attribute, and `reset(key, value)` to read or persist `size_limit`, `cull_limit` and `eviction_policy` (a `FanoutCache` splits a `size_limit` total across its shards), on `Cache`, `FanoutCache` and `NullCache`.
- Pickling a `Cache` or `FanoutCache`: it reopens the directory with the options the handle was opened with, in another process too. A `TypeRegistry` pickles (the global one by reference).
- `stats(enable=True, reset=False)`: hit and miss counts of `get`, per process and in memory, so a `get` never writes.
- `check(fix=False, retry=False)`: the database and value files against each other, as diskcache's, in short steps; `fix=True` removes entries whose files are missing or cut short and old orphan files, sweeps empty folders, corrects the totals and `VACUUM`s. `UnknownFileWarning` and `EmptyDirWarning`.
- The coordination recipes `Lock`, `RLock`, `BoundedSemaphore`, `Averager`, `throttle`, `barrier` and `memoize_stampede`, as diskcache's, refusing a `NullCache` or `durable=True` cache, with `throttle`'s two diskcache bugs fixed. See the new Coordination guide page.
- Tags: `tag=` (keyword-only) on `set` and `add`, any key type, compared by exact type, and `tag=True` on `get` and `pop`, returning diskcache's `(value, tag)` and `(value, expire_time, tag)` tuples. `evict(tag, *, timeout=None, retry=False)` on `Cache`, `FanoutCache` and `NullCache` removes every entry with that tag stored before the call, in chunks like `clear()`. `memoize(tag=...)` and the recipes' `tag=` tag what they store. An overwrite without `tag=` leaves the entry untagged; `add` on a live key, `touch` and `incr` keep its tag.
- `eviction_policy='least-recently-stored'` (the default) or `'none'` on `Cache` and `FanoutCache`, stored in the cache and read inside every write, so a change from one process applies to all at their next write. Under `'none'` nothing is evicted for size, expired entries are still removed, and `cull()` returns how many it removed. `'least-recently-used'` and `'least-frequently-used'` raise `ValueError`. A read-only `eviction_policy` attribute.
- The queue calls `push`, `pull`, `peek` and `peekitem` on `Cache` and `NullCache`, with diskcache's signatures and return shapes (`expire_time=`, `tag=`, `retry=` keyword-only). Each item goes to exactly one puller across every process; `pull` decodes before it deletes, so an item this process can't read stays queued; `peek` and `peekitem` never write. A queue's items are only keys of its exact shape, and `push` raises `OverflowError` rather than overwrite a key. See the new Work queues recipe.
- `ENOVAL` and `UNKNOWN`, diskcache's sentinels.
- `retry=True` (keyword-only) on `get`, `set`, `add`, `delete`, `pop`, `touch`, `incr`, `decr`, `expire`, `cull`, `clear` and `evict`: this cache's own `Timeout` is retried until the call gets through, even on a `durable=True` handle; one raised by your code (a hook, a `clock`) is not. A retried `expire`, `cull`, `clear` or `evict` returns everything it removed.

### Changed

- Iterating a cache is typed `Iterator[Key]` rather than `Iterator[str]`, since keys can now be other types; `iterkeys(prefix=...)` is still `Iterator[str]`. Type-checked code that treats every iterated key as a `str` needs a narrowing (`isinstance(key, str)`).
- The `TypeError` for a key of the wrong type now names the types that are accepted.
- `TransactionalCacheLike` gained `push`, `pull`, `peek` and `peekitem` (not `CacheLike`: `FanoutCache` has no queue calls).
- `CacheLike` gained `memoize`, `stats`, `check`, `reset`, `evict`, `durable`, `eviction_policy` and `__reversed__`, `get` and `pop` their `tag=` (and `pop` its `expire_time=`), and `set` and `add` theirs; a fake typed against it needs them too.
- `transact(retry=True)` sleeps between attempts instead of retrying at once, so a `timeout=0` cache doesn't spin.

## [0.1.2] - 2026-11-09

The first release: diskcache's `Cache` API on a Rust engine and SQLite storage, shared safely by threads, processes and `fork()`, on CPython 3.14 and 3.15 including the free-threaded builds. Wheels for macOS on Apple Silicon and Linux (glibc) on x86_64 and aarch64.

### Added

- `Cache`, API compatible with diskcache 5.6.3's `Cache`: `get`, `set`, `add`, `incr`/`decr`, `pop`, `delete`, `touch`, `expire`, `cull`, `clear`, `iterkeys`, `len`, `volume`, size limits with least-recently-stored eviction, and the mapping methods. It passes a conformance suite run against diskcache itself, apart from the [documented divergences](https://mkennedy.codes/docs/diskcache-oxide/user-guide/divergences.html).
- `FanoutCache`, which shards keys over several databases so many writing processes don't queue on one write lock.
- `transact()` on both, which lands several writes together or none of them, with `TransactionBusy` and `TransactionAborted`.
- Values stored as msgpack (the default) or JSON, with exact type tags for tuples, sets, datetimes, big ints and NaN. A value that can't round-trip exactly raises `TypeError` at `set()`. `register_type()` stores your own types and a library's without pickle, a dataclass with only a name; `pickle=True` is opt-in per cache, and a cache without it never unpickles.
- `durable=True`, which answers an outage (a busy lock, a full disk) as a miss and logs it at most once a minute, and `on_decode_error=` for values that can't be read.
- `NullCache`, which turns caching off without changing call sites, and `Caches`, a registry that names an app's caches, opens each on first use and hands out a `NullCache` while one can't open.
- Fork safety: a process-wide fork barrier so no child is born holding half a transaction, and caches that keep working in the child.
- Bounded resources: at most `max_connections` SQLite connections per cache directory, a page cache per connection, a write-ahead log that trims itself back to half of `wal_size_limit`, and `checkpoint()`.
- `CacheLike`, `TransactionalCacheLike` and `ShardedCacheLike` protocols, `py.typed`, and full docstrings in the stubs.
- A bundled SQLite (`sqlite_version()`, `sqlite_compile_options()`), so the system's version doesn't matter.
- Documentation: a user guide, recipes, an errors page, an API reference, an agent skill and `llms.txt`, at <https://mkennedy.codes/docs/diskcache-oxide/>.

### Differences from diskcache

- Not data compatible: data lives in `diskcache-oxide.sqlite`, and diskcache's `cache.db` is never opened or converted, so a switched app starts cold.
- Keys are `str` only, `directory` is required, `size_limit` must be at least 1 MiB, new cache directories are owner-only (`0700`), `Timeout` is a `TimeoutError`, and `bool(cache)` is always True.
- `clear()` keeps writes that land while it runs, `expire()` removes every expired entry, and a read during an overwrite never misses. Each is a diskcache bug, with a standalone repro in the repository's `upstream/` folder.

### Not yet

`memoize`, tags, `AsyncCache`, `Deque` and `Index`, keys other than `str`, `disk=`, other eviction policies, the coordination recipes (locks, semaphores, throttles), and Windows. See the [roadmap](https://mkennedy.codes/docs/diskcache-oxide/user-guide/roadmap.html).

Development before this release, including the `0.1.0.dev` builds (`0.1.0.dev1` was yanked from PyPI), is in the git history. A `0.1.0` and a `0.1.1` were built and verified but never published: new tests found two bugs just before the first upload and a third (a size-limited cache evicting entries it had room for) just before the second, each fixed first, so `0.1.2` is the first release on PyPI. `0.1.1` ran in production at Talk Python in the meantime. The `0.0.1` on PyPI is a name placeholder with no code.

[Unreleased]: https://github.com/mikeckennedy/diskcache-oxide/compare/v0.4.7...HEAD
[0.4.7]: https://github.com/mikeckennedy/diskcache-oxide/compare/v0.4.6...v0.4.7
[0.4.6]: https://github.com/mikeckennedy/diskcache-oxide/compare/v0.3.5...v0.4.6
[0.3.5]: https://github.com/mikeckennedy/diskcache-oxide/compare/v0.2.3...v0.3.5
[0.2.3]: https://github.com/mikeckennedy/diskcache-oxide/compare/v0.1.2...v0.2.3
[0.1.2]: https://github.com/mikeckennedy/diskcache-oxide/releases/tag/v0.1.2
