# Changelog

This changelog is generated automatically from [GitHub Releases](https://github.com/mikeckennedy/diskcache-oxide/releases).


# v0.1.0

*2026-11-09* · [GitHub](https://github.com/mikeckennedy/diskcache-oxide/releases/tag/v0.1.0)

The first release: diskcache's [Cache](reference/Cache.html#diskcache_oxide.Cache) API on a Rust engine and SQLite storage, shared safely by threads, processes and `fork()`, on CPython 3.14 and 3.15 including the free-threaded builds. Wheels for macOS on Apple Silicon and Linux (glibc) on x86_64 and aarch64.


### Added

- [Cache](reference/Cache.html#diskcache_oxide.Cache), API compatible with diskcache 5.6.3's [Cache](reference/Cache.html#diskcache_oxide.Cache): [get](reference/Cache.get.html#diskcache_oxide.Cache.get), [set](reference/Cache.set.html#diskcache_oxide.Cache.set), [add](reference/Cache.add.html#diskcache_oxide.Cache.add), [incr](reference/Cache.incr.html#diskcache_oxide.Cache.incr)/[decr](reference/Cache.decr.html#diskcache_oxide.Cache.decr), [pop](reference/Cache.pop.html#diskcache_oxide.Cache.pop), [delete](reference/Cache.delete.html#diskcache_oxide.Cache.delete), [touch](reference/Cache.touch.html#diskcache_oxide.Cache.touch), [expire](reference/Cache.expire.html#diskcache_oxide.Cache.expire), [cull](reference/Cache.cull.html#diskcache_oxide.Cache.cull), [clear](reference/Cache.clear.html#diskcache_oxide.Cache.clear), [iterkeys](reference/Cache.iterkeys.html#diskcache_oxide.Cache.iterkeys), `len`, [volume](reference/Cache.volume.html#diskcache_oxide.Cache.volume), size limits with least-recently-stored eviction, and the mapping methods. It passes a conformance suite run against diskcache itself, apart from the [documented divergences](https://mkennedy.codes/docs/diskcache-oxide/user-guide/divergences.html).
- [FanoutCache](reference/FanoutCache.html#diskcache_oxide.FanoutCache), which shards keys over several databases so many writing processes don't queue on one write lock.
- [transact()](reference/Cache.transact.html#diskcache_oxide.Cache.transact) on both, which lands several writes together or none of them, with [TransactionBusy](reference/TransactionBusy.html#diskcache_oxide.TransactionBusy) and `TransactionAborted`.
- Values stored as msgpack (the default) or JSON, with exact type tags for tuples, sets, datetimes, big ints and NaN. A value that can't round-trip exactly raises `TypeError` at [set()](reference/Cache.set.html#diskcache_oxide.Cache.set). `register_type()` stores your own types and a library's without pickle, a dataclass with only a name; `pickle=True` is opt-in per cache, and a cache without it never unpickles.
- `durable=True`, which answers an outage (a busy lock, a full disk) as a miss and logs it at most once a minute, and `on_decode_error=` for values that can't be read.
- [NullCache](reference/NullCache.html#diskcache_oxide.NullCache), which turns caching off without changing call sites, and [Caches](reference/Caches.html#diskcache_oxide.Caches), a registry that names an app's caches, opens each on first use and hands out a [NullCache](reference/NullCache.html#diskcache_oxide.NullCache) while one can't open.
- Fork safety: a process-wide fork barrier so no child is born holding half a transaction, and caches that keep working in the child.
- Bounded resources: at most `max_connections` SQLite connections per cache directory, a page cache per connection, a write-ahead log that trims itself back to half of `wal_size_limit`, and [checkpoint()](reference/Cache.checkpoint.html#diskcache_oxide.Cache.checkpoint).
- [CacheLike](reference/CacheLike.html#diskcache_oxide.CacheLike), [TransactionalCacheLike](reference/TransactionalCacheLike.html#diskcache_oxide.TransactionalCacheLike) and [ShardedCacheLike](reference/ShardedCacheLike.html#diskcache_oxide.ShardedCacheLike) protocols, `py.typed`, and full docstrings in the stubs.
- A bundled SQLite ([sqlite_version()](reference/sqlite_version.html#diskcache_oxide.sqlite_version), [sqlite_compile_options()](reference/sqlite_compile_options.html#diskcache_oxide.sqlite_compile_options)), so the system's version doesn't matter.
- Documentation: a user guide, recipes, an errors page, an API reference, an agent skill and `llms.txt`, at <https://mkennedy.codes/docs/diskcache-oxide/>.


### Differences from diskcache

- Not data compatible: data lives in `diskcache-oxide.sqlite`, and diskcache's `cache.db` is never opened or converted, so a switched app starts cold.
- Keys are `str` only, [directory](reference/Cache.directory.html#diskcache_oxide.Cache.directory) is required, [size_limit](reference/Cache.size_limit.html#diskcache_oxide.Cache.size_limit) must be at least 1 MiB, new cache directories are owner-only (`0700`), [Timeout](reference/Timeout.html#diskcache_oxide.Timeout) is a `TimeoutError`, and `bool(cache)` is always True.
- [clear()](reference/Cache.clear.html#diskcache_oxide.Cache.clear) keeps writes that land while it runs, [expire()](reference/Cache.expire.html#diskcache_oxide.Cache.expire) removes every expired entry, and a read during an overwrite never misses. Each is a diskcache bug, with a standalone repro in the repository's `upstream/` folder.


### Not yet

`memoize`, tags, `AsyncCache`, `Deque` and `Index`, keys other than `str`, `disk=`, other eviction policies, the coordination recipes (locks, semaphores, throttles), and Windows. See the [roadmap](https://mkennedy.codes/docs/diskcache-oxide/user-guide/roadmap.html).

Development before this release, including the `0.1.0.dev` builds (`0.1.0.dev1` was yanked from PyPI), is in the git history. The `0.0.1` on PyPI is a name placeholder with no code.
