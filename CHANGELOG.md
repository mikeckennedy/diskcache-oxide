# Changelog

All notable changes to diskcache-oxide are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [semantic versioning](https://semver.org/). During 0.x, a release that changes the on-disk format bumps the minor version and says so first in its entry; caches made by an older format must be deleted when you upgrade to it.

## [Unreleased]

No cache needs deleting: the on-disk format is unchanged, and 0.1.2 and this release can share a cache directory during a deploy. 0.1.2 never sees the new key types in its lookups or iteration, though its `len()` counts them, and its `expire()`, `cull()` and `clear()` remove them as usual.

### Added

- Keys of every native type: `bytes`, `int` (64-bit), `float`, `bool`, `None`, and tuples of those, nested up to 32 deep, beside `str`. They're never pickled and compare by exact type, so `1`, `1.0` and `True` are three keys. `iterkeys()` lists `str` keys first, then the other types in a fixed order; `iterkeys(prefix=...)` lists `str` keys only. A FanoutCache routes every key type. Type checkers see `diskcache_oxide.Key`.
- `memoize(name=None, typed=False, expire=None, ignore=())` on `Cache`, `FanoutCache` and `NullCache`, as diskcache's, with the same keys, so `f.__cache_key__(...)` returns the tuple diskcache would. Every argument must be a key type, checked before the function runs; `typed=True` stores type names. `Memoized` and `Memoizer` type the wrapper.
- `CorruptKeyError`, a `CorruptValueError`, raised by iteration that meets a stored key it can't read (a damaged page or file); it never skips one.
- `retry=True` (keyword-only) on `get`, `set`, `add`, `delete`, `pop`, `touch`, `incr`, `decr`, `expire`, `cull` and `clear`: this cache's own `Timeout` is retried until the call gets through, even on a `durable=True` handle; one raised by your code (a hook, a `clock`) is not. A retried `expire`, `cull` or `clear` returns everything it removed.

### Changed

- Iterating a cache is typed `Iterator[Key]` rather than `Iterator[str]`, since keys can now be other types; `iterkeys(prefix=...)` is still `Iterator[str]`. Type-checked code that treats every iterated key as a `str` needs a narrowing (`isinstance(key, str)`).
- The `TypeError` for a key of the wrong type now names the types that are accepted.
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

[Unreleased]: https://github.com/mikeckennedy/diskcache-oxide/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/mikeckennedy/diskcache-oxide/releases/tag/v0.1.2
