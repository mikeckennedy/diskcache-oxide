# Changelog

All notable changes to diskcache-oxide are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions are MAJOR.MINOR.BUILD, which isn't semantic versioning: MINOR goes up for new features and behavior changes, MAJOR for breaking ones, and BUILD counts releases and never resets. During 0.x, a release that changes the on-disk format bumps the minor version and says so first in its entry. Since 0.3.5, a cache in another format opens a new cache in a versioned subdirectory instead of failing, and `remove_old_formats()` cleans up the old one (see [Upgrading across a format change](https://mkennedy.codes/docs/diskcache-oxide/recipes/upgrading.html)). The last number goes up with every release, a minor bump included, so 0.1.2 is followed by 0.2.3.

## [Unreleased]

**Upgrades across a format change no longer fail.** A cache directory that holds a cache in another on-disk format (made by an older release, or by a newer one before a rollback) now opens a new, empty cache in its subdirectory `diskcache-oxide-format-N`, N being this release's format (3), instead of raising `FormatMismatchError`, and logs a warning on the `diskcache_oxide` logger once per process. Of the other format's cache, only its format is read, and its contents are never changed (if that read is the last connection to close on it, SQLite may checkpoint its write-ahead log into the database file, which changes no data). Once the subdirectory exists, every later open uses it. A `FanoutCache` moves as a whole. This release keeps format 3, so caches made by 0.2.3 open where they are, warm; a cache still at format 2 (0.1.2) opens `diskcache-oxide-format-3` beside it. See "Upgrading across a format change" in the recipes.

### Added

- `remove_old_formats()` on `Cache`, `FanoutCache` and `NullCache` (where it returns `[]`), and on `Caches` for every registered cache: removes caches in an older format from the cache's directory, only files diskcache-oxide wrote, never following links, never a newer or the current format, and returns what it removed. It doesn't check whether an older worker still uses those files: call it once they've all stopped.

- Windows on x86_64, with wheels for CPython 3.14, 3.14t, 3.15 and 3.15t. The API and the on-disk format are the same; caches made on macOS or Linux aren't affected. Windows has no `fork()`, so the fork guarantees are Unix-only. Getting started has a new Windows section: an open cache's directory can't be deleted, new directories inherit their parent's permissions, and a value file another program holds past about 50 ms of retries is left until `check(fix=True)` removes it, an hour after it was written. SQLite uses its own Win32 mutexes there (`_sqlite_mutex_status()` reports `'native'`).

### Changed

- A cache directory holding another on-disk format opens the `diskcache-oxide-format-N` subdirectory with a warning instead of raising `FormatMismatchError` (D-059). `FormatMismatchError` is now raised only for another format inside that subdirectory. `directory` on `Cache` and `FanoutCache` reports where the cache really is, the subdirectory after such a move; a pickled handle keeps the directory as passed and resolves it again. A `Caches` registry no longer hands out a `NullCache` for a format change.
- A plain `Cache` opened on a `FanoutCache` shard directory in an older format raises `LayoutMismatchError`, as it always did for a current one, rather than `FormatMismatchError`.
- `FanoutCache(directory, shards=n)` on a root that records another count no longer raises: it opens with the stored count and logs a `WARNING` once per process. The new keyword `raise_on_mismatch=True` restores the `LayoutMismatchError`, and `Caches.register(..., raise_on_mismatch=True)` turns it into an open failure (a reported `NullCache`). A root in another on-disk format redirects to its versioned subdirectory either way. The `LayoutMismatchError` for settings that don't fit the stored count now says `shards=n was asked for` rather than `is registered with`, since a plain `FanoutCache` raises it too.

### Fixed

- `import diskcache_oxide` no longer calls `os.register_at_fork` where it doesn't exist.
- A value file that hits a full quota (`EDQUOT`) now raises `OSError` with `errno.ENOSPC`, as a full disk does, since a full disk is recognized by the error's kind rather than one Unix error number.

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

[Unreleased]: https://github.com/mikeckennedy/diskcache-oxide/compare/v0.2.3...HEAD
[0.2.3]: https://github.com/mikeckennedy/diskcache-oxide/compare/v0.1.2...v0.2.3
[0.1.2]: https://github.com/mikeckennedy/diskcache-oxide/releases/tag/v0.1.2
