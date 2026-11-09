# Changelog

All notable changes to diskcache-oxide are recorded here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [semantic versioning](https://semver.org/). During 0.x, a release that changes the on-disk format bumps the minor version and says so first in its entry; caches made by an older format must be deleted when you upgrade to it.

## [Unreleased]

### Added

- A user guide page for readers new to caching: what a cache is, why it's worth it, the vocabulary, invalidation and key naming, and where a directory cache sits between an in-process dict and a cache server.
- Seven more recipes in the documentation: serving stale values while refreshing, caching responses from other services, storing your own types, counting things, single-use keys, server-side sessions, and using the cache from asyncio. The web workers recipe gains Django and Celery sections.
- The documentation's version badge and Changelog page now come from the repository's `Cargo.toml` and `CHANGELOG.md` at build time, so they describe the build rather than the latest GitHub Release.
- The benchmark tables in the FanoutCache, performance and roadmap pages are now drawn as interactive charts in the browser, from the tables themselves, so rerunning a benchmark means editing the table and nothing else. The tables stay in the page, the markdown mirrors and `llms-full.txt`.

### Fixed

- A read of a large value that kept losing the race to writers overwriting it could report a miss when the last of those overwrites was small, though the key was present the whole time. It now returns that value. It took eight lost races in a row, and the cost was one recompute, never a wrong value.

## [0.1.0] - 2026-11-09

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

Development before this release, including the `0.1.0.dev` builds (`0.1.0.dev1` was yanked from PyPI), is in the git history. The `0.0.1` on PyPI is a name placeholder with no code.

[Unreleased]: https://github.com/mikeckennedy/diskcache-oxide/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/mikeckennedy/diskcache-oxide/releases/tag/v0.1.0
