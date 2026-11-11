---
name: diskcache-oxide
description: >-
  Use diskcache-oxide, a shared, durable disk cache for Python with a Rust engine, SQLite storage and python-diskcache's API, built for many threads, many worker processes and free-threaded CPython. Use when adding or configuring diskcache_oxide in an app (Cache, FanoutCache, NullCache, Caches), switching code from diskcache, choosing between Cache and FanoutCache or between raising, missing and switching caching off, writing tests around a cache, or when code raises Timeout, TransactionBusy, UnsafeValueError, CorruptValueError, LayoutMismatchError, FormatMismatchError, or "can't be stored without pickle".
license: MIT
compatibility: Requires CPython 3.14 or 3.15 (GIL or free-threaded) on macOS or Linux. SQLite is bundled; no runtime dependencies.
---

# diskcache-oxide

A cache that is a directory: every thread and process that opens the same path shares one cache, and it survives restarts. It keeps python-diskcache's `Cache` API and rebuilds the engine in Rust on SQLite: reads in under a microsecond, writes atomic across processes, safe across `fork()`, no pickle unless asked for, bounded memory and disk. Measured against the defunct python-diskcache 5.6.3 on the same machine: a cached `get` in 0.76 µs at p50 (0.42× diskcache's), and at 8 threads sharing one `Cache` about 650,000 operations a second where diskcache falls to 19,000; eight writing processes on a `FanoutCache` reach about 1.5 million a second. It is not data compatible with diskcache (it uses `diskcache-oxide.sqlite`, never `cache.db`). Errors an agent will meet: `TypeError` at `set()` for a value the formats can't represent, `Timeout` (a `TimeoutError`) when a lock stays busy, `UnsafeValueError` when a `pickle=False` cache reads a pickled row, and `LayoutMismatchError` when a directory holds the other kind of cache.

## Install

Install with `pip install diskcache-oxide` (CPython 3.14 or 3.15, GIL or free-threaded; wheels for macOS arm64 and Linux x86_64/aarch64, elsewhere a source build that needs the latest stable Rust). The import is `diskcache_oxide`. During 0.x a release that changes the on-disk format bumps the minor version; caches from the older format raise `FormatMismatchError` and must be deleted.

## Use

```python
from diskcache_oxide import Cache, Timeout

# Open once per process and share it across threads; every worker opens the same path.
cache = Cache('/var/cache/my-app', timeout=1.0)

cache.set('episodes:popular', episodes, expire=300)  # always give cached values an expire
episodes = cache.get('episodes:popular')  # None (or default=) on a miss

# Atomic across processes: exactly one caller gets True.
if cache.add('job:refresh', os.getpid(), expire=3600):
    refresh()

try:
    cache.set(key, value, expire=60)
except Timeout:  # the write lock stayed busy for `timeout`: skip the cache, never fail the request
    pass
```

An app with several caches registers them once and looks them up by name; a cache that can't open comes back as a `NullCache` and is retried after 10 s:

```python
caches = Caches('/var/cache/my-app', timeout=1.0, durable=True)
caches.register('pages', size_limit=2 * 2**30)
caches.register('rate_limits', shards=8)  # a FanoutCache
caches['pages'].get('/about')
caches.fanout('rate_limits').incr('ip:203.0.113.7')
```

In tests: `Cache(tmp_path / 'cache')` per test, `NullCache()` where caching must not interfere, and `Cache(tmp_path, clock=fake_clock)` to test expiry without sleeping. If tests patch `time.time`, pass `clock=lambda: time.time()`, since the cache reads the clock natively.

## Rules

- Keys are `str`, `bytes`, `int` (64-bit), `float`, `bool`, `None`, or tuples of those, never pickled; a `list`, `dict`, object, subclass (`IntEnum`, `namedtuple`) or int past 64 bits is `TypeError`, a NaN `ValueError`. They compare by exact type: `1`, `1.0` and `True` are three keys. For a prefix scan use `str` keys with a trailing separator (`user:42:` so `user:4` doesn't match `user:42`): `prefix=` sees only `str` keys.
- Values are exact built-ins only: `str`, `bytes`, `int`, `float`, `Decimal`, `bool`, `None`, and `list`, `tuple`, `dict`, `set`, `frozenset`, `datetime` and `date` made of those, all round-tripped exactly (tuples stay tuples, big ints and NaN survive). A dataclass, ORM row or `dict` subclass raises `TypeError` at `set()`. Register the type instead: `diskcache_oxide.register_type(Episode, name='app.Episode')` for a dataclass (fields stored, rebuilt with `Episode(**fields)`), or `register_type(ObjectId, name='bson.ObjectId', encode=str, decode=ObjectId)` for anything else, at import time in every process. The name is the tag on disk; a reader without the registration gets `ForeignValueError` and the row stays. A registered type is never pickled. If arbitrary objects are really needed, give them their own `Cache(dir, pickle=True)`.
- A `pickle=False` cache never unpickles: reading a row a `pickle=True` handle wrote raises `UnsafeValueError` (a `TypeError`) and keeps the row. This is a configuration mistake, not damage, so it raises under every `on_decode_error` and `durable` setting.
- `bool(cache)` means "caching is on" (`Cache` and `FanoutCache` are always truthy, `NullCache` is falsy). Never use `if cache:` for "is it empty"; use `len(cache)`.
- `Timeout` subclasses `TimeoutError`. The default `timeout` is 60 s; in a request path use `timeout=1.0` and treat `Timeout` as "don't cache this time". A fork counts against `timeout`, so don't go below about 0.1 s in a pre-forking server.
- `expire=True` and `timeout=True` raise `TypeError` (a bool there is an argument in the wrong position). `size_limit` under 1 MiB raises `ValueError`. `directory` is required.
- Open a cache once per process and share it across threads. The same directory opened twice in one process shares one connection pool. Use `with Cache(...) as cache:` or call `close()`; a closed handle reopens on its next use. Forks need nothing from you: a fork barrier waits for other threads' writes and the child opens fresh connections.
- `add()` is the claim primitive: one winner across processes. A `NullCache.add()` always returns True and a `durable=True` `add()` returns False on an outage, so keep job claims and locks on a plain handle in their own small directory, with an `expire`.
- `incr()`/`decr()` are atomic across processes and keep the entry's existing expiry; `incr()` on a missing key starts from `default` (0) and creates an entry that never expires. For a windowed counter, `add(key, 0, expire=2 * window)` then `incr(key)`. `default=None` makes a missing key a `KeyError`.
- `iterkeys(prefix=...)` is an indexed range scan in sorted order, safe to delete from while iterating. Without a prefix it lists `str` keys first, then `None`, bools, ints, floats, bytes and tuples, each sorted. `iter(cache)` is insertion order. `len()` and iteration include expired entries not yet swept; `get` and `in` never do.
- `with cache.transact() as tx:` groups writes on the calling thread; `tx` is the cache itself. Commit on exit, roll back on an exception, a nested block joins the outer one, a block whose writes were lost raises `TransactionAborted` (a `RuntimeError`) at exit. Never `await` inside the block. Beginning a transaction on a second cache whose lock another thread holds raises `TransactionBusy` at once, and `retry=True` retries only `Timeout`. On a `FanoutCache`, `transact(key=k)` locks one shard, and inside it any call for a key on another shard, or over every shard (`len`, `volume`, iteration, `clear`, `expire`, `cull`, `checkpoint`), raises `ValueError`; a bare `transact()` there is a `TypeError`.
- `durable=True` is "the cache can't take a request down", not database durability. On an outage a single-key call returns its miss value (`get` the default, `set` False, `incr` `default + delta`) and logs once a minute on the `diskcache_oxide` logger. It can drop writes, so a dropped `set` or `delete` serves stale data until the entry expires. The constructor, admin calls (`clear`, `expire`, `cull`, `checkpoint`, `len`, `volume`, iteration), transactions and bugs (`TypeError`, `UnsafeValueError`) still raise. Pair it with a small `timeout`.
- `on_decode_error='miss'` (the default under `durable=True`) makes a value that can't be decoded a miss: the row is deleted and a warning logged. A row a newer version or another host might read (`ForeignValueError`) is a miss that keeps the row. A function in its place is called as `report(key, exc)`; from a `Caches` registry it is `report(name, key, exc)`.
- `NullCache(invalidate=real)` forwards only removals (`delete`, `pop`, `clear`, `iterkeys`) to the real cache. An overwriting `set` or a version-bump `incr` is dropped while caching is off, so invalidate by deleting.
- `Caches`: register every cache at startup, before handing any out. `caches[name]` reads a plain cache and `caches.fanout(name)` one registered with `shards=` (the other way is a `TypeError`). Passing `shards=` at all, even `shards=None`, registers a `FanoutCache`. A registered `shards=n` applies only to a new folder: an existing folder keeps its own count, and the registry logs a WARNING (and one ERROR per process) instead of failing; to change the count, delete the folder with every worker stopped. `disable()`/`enable()` change later lookups only.
- `FanoutCache` is for several processes that write often. `shards` is keyword-only (`FanoutCache(path, 4)` is `timeout=4`), the count is recorded and permanent for the directory (`LayoutMismatchError` otherwise), a plain `Cache` and a `FanoutCache` refuse each other's directories, `max_connections` and `size_limit` are totals (at least `shards` connections and `shards` MiB). It does not make threads sharing one handle faster.
- Cache directories must be on a local disk: SQLite's WAL mode is required and opening on NFS and similar fails with a `RuntimeError` naming the journal mode. New directories are created `0700`.
- After a big fill or `clear()`, call `cache.checkpoint()` so the write-ahead log is truncated; `close()` does it for a large log; `wal_checkpoint='auto'` is the opt-in for long-running writers. Put maintenance (`expire()`, `checkpoint()`) in one scheduled job, not in every worker.
- `@cache.memoize(name=None, typed=False, expire=None, tag=None, ignore=())` caches a function's results, with diskcache's key shape (`f.__cache_key__(*args, **kwargs)` gives it, for `cache.delete(...)`). Call it with parentheses. `ignore` is the fifth positional argument (after `tag`), so pass it by name. Every argument not in `ignore` must be a key type, or the call raises before the function runs; put connections and clients in `ignore`. `expire=0` never stores; `None` results are cached; `async def` is refused. On a `NullCache` it runs every call.
- `retry=True` (keyword-only) on `get`, `set`, `add`, `delete`, `pop`, `touch`, `incr`, `decr`, `expire`, `cull`, `clear` keeps retrying a `Timeout` until it gets through, even under `durable=True`. Use it for background jobs, not request paths. `cache[k]` never retries.
- Coordination: `Lock(cache, key, expire=...)`, `RLock`, `BoundedSemaphore(cache, key, value=n)`, `Averager`, `@throttle(cache, count, seconds)`, `@barrier(cache, Lock)` and `@memoize_stampede(cache, expire)`, from `diskcache_oxide`, atomic across processes. Use a small cache of their own (culling and `clear()` remove lock keys); give a `Lock` an `expire`. They raise `TypeError` on a `NullCache` or `durable=True` cache (`memoize_stampede` doesn't), and `RuntimeError` if they'd wait inside a `transact()` on their own cache.
- `stats(enable=True, reset=False) -> (hits, misses)` counts `get` per process, in memory. `check(fix=False)` returns a list of `warnings.WarningMessage`; `fix=True` removes broken entries and old orphan files, then `VACUUM`s (slow on a big cache). `reset('size_limit' | 'cull_limit', value)` reads or persists a limit. `reversed(cache)`, `pop(key, expire_time=True)`, and pickling a cache (it reopens the directory) work.
- Tags: `set(k, v, tag='user:42')` (`tag=` is keyword-only, any key type, compared by exact type), `get(k, tag=True)` returns `(value, tag)`, and `evict('user:42')` removes every entry with that tag; an overwrite without `tag=` clears the tag. `eviction_policy='none'` (keyword-only, stored in the cache) never evicts for size: use it for locks, claims and anything that must not vanish, and give those entries an `expire`. `'least-recently-used'` and `'least-frequently-used'` raise `ValueError`.
- Not available yet, so don't generate calls to them: `Deque`, `Index`, `AsyncCache`, `FanoutCache.cache()`/`deque()`/`index()`, `read()`/`read=`, and Windows. LRU/LFU eviction and `disk=` aren't coming.

## Decision table

| Need | Use |
|---|---|
| One process, or mostly reads | `Cache(directory)` |
| Several worker processes that write often (counters, rate limits, claims) | `FanoutCache(directory, shards=8)` |
| Many caches in one app, an admin page, switching one off by name | `Caches(base_dir, timeout=1.0, ...)` then `register(name, ...)` |
| Caching off in tests, local dev or while chasing a stale-data bug | `NullCache()`, or `NullCache(invalidate=real)` so deletes still land |
| A cache outage must cost speed, never a request | `Cache(..., timeout=1.0, durable=True)` |
| A value that can't be decoded should be a miss, not an exception | `on_decode_error='miss'` or `on_decode_error=report` |
| Store a dataclass, an `ObjectId`, a namedtuple, a `str` subclass | `register_type(cls, name=..., encode=..., decode=...)`; a dataclass needs only `name=` |
| Cache arbitrary objects | register the type; otherwise a separate `Cache(dir, pickle=True)` |
| Inspect rows by hand in a SQLite browser | `serializer='json'` |
| Several writes that must land together | `with cache.transact() as tx:`; on a `FanoutCache`, `transact(key=k)` |
| Exactly one worker runs a scheduled job | `cache.add('job:x', os.getpid(), expire=interval - margin)` on a plain, never-null handle |
| Per-client rate limit shared by every worker | `add(f'rate:{client}:{window_no}', 0, expire=2 * window)` then `incr(...) <= limit` |
| Invalidate everything about one user or object | `iterkeys(prefix='user:42:')` plus `delete`, or a generation number in the key |
| Tests of expiry without sleeping; tests that patch `time.time` | `Cache(tmp_path, clock=fake)`; `clock=lambda: time.time()` |
| A huge `-wal` file after a write burst | `cache.checkpoint()`, or `Cache(..., wal_checkpoint='auto')` |
| Type a function that accepts any cache | `CacheLike`; `TransactionalCacheLike` if it calls `transact()`; `ShardedCacheLike` for `transact(key=)` |

## API

- `Cache(directory, timeout=None, *, size_limit=None, cull_limit=None, serializer='msgpack', pickle=False, types=None, clock=None, max_connections=None, sqlite_cache_size=None, wal_size_limit=None, wal_checkpoint='manual', on_decode_error=None, durable=False)`: raises `ValueError`/`TypeError` for bad settings, `LayoutMismatchError` or `FormatMismatchError` for a directory this build can't use, `Timeout` if another process holds it locked. Defaults: `timeout` 60 s, `size_limit` 1 GiB, `cull_limit` 10, `max_connections` 8, `wal_size_limit` 64 MiB.
- `get(key, default=None, *, expire_time=False, tag=False, retry=False)`, `set(key, value, expire=None, *, tag=None, retry=False) -> bool`, `add(key, value, expire=None, *, tag=None, retry=False) -> bool`, `delete(key, *, retry=False) -> bool`, `pop(key, default=None, *, expire_time=False, tag=False, retry=False)`, `touch(key, expire=None, *, retry=False) -> bool`, `incr(key, delta=1, default=0, *, retry=False)`, `decr(...)`, `memoize(name=None, typed=False, expire=None, tag=None, ignore=())`, `iterkeys(*, prefix=None, reverse=False)`, `expire(*, timeout=None, retry=False) -> int`, `clear(*, timeout=None, retry=False) -> int`, `evict(tag, *, timeout=None, retry=False) -> int`, `cull(*, timeout=None, retry=False) -> int`, `reset(key, value=ENOVAL)`, `volume() -> int`, `checkpoint(*, timeout=None) -> bool` (False when busy, never raises for that), `close(*, checkpoint=True)`, `transact(retry=False)`; plus `cache[key]`, `cache[key] = value` (never expires), `del cache[key]`, `key in cache`, `len(cache)`, `iter(cache)`, `with`. Properties: `directory`, `timeout`, `size_limit`, `cull_limit`, `eviction_policy`, `enabled`, `durable`.
- `FanoutCache(directory, timeout=None, *, shards=None, ...)` with the same settings and single-key methods, plus `shards`, `shard_of(key) -> int` and `transact(*, key, retry=False)`. Its `clear`/`expire`/`cull`/`checkpoint` share one `timeout` across the shards and, on failure, raise with `failed_shards` and `removed` attributes.
- `NullCache(directory=None, timeout=None, *, ..., invalidate=None)`: accepts and ignores `Cache`'s settings; `get`/`pop` return the default, `set`/`add` return True, `incr` returns `default + delta`, everything else is 0, False or empty; `transact()` is a no-op yielding itself.
- `Caches(base_dir, *, on_open_error=None, retry_open_after=10.0, **defaults)`: `register(name, *, directory=None, **settings)`, `caches[name]`, `fanout(name)`, `disable(*names)`, `enable(*names)`, `enabled(name)`, `stats() -> dict[name, CacheStats]`, `clear(name, *, timeout=None)` (raises if it can't open), `expire_all(*, timeout=None)`, `close_all()`, `with`.
- Protocols: `CacheLike`, `TransactionalCacheLike` (adds `transact(retry=False)`), `ShardedCacheLike` (adds `transact(*, key, retry=False)`). `Cache` and `NullCache` are transactional; `FanoutCache` and `NullCache` are sharded.
- `register_type(cls, *, name, encode=None, decode=None)`, `unregister_type(cls) -> bool`, `registered_types() -> dict[str, type]`, on the process-wide `registry: TypeRegistry`; `TypeRegistry()` with the same `register`/`unregister`/`registered` for one cache's `types=`. Own built-in types can't be registered (`ValueError`); one of `encode`/`decode` alone, a non-dataclass with neither, or a dataclass with an `InitVar` is `TypeError`.
- Exceptions: `Timeout(TimeoutError)`, `TransactionBusy(Timeout)`, `TransactionAborted(RuntimeError)`, `UnsafeValueError(TypeError)`, `CorruptValueError(ValueError)`, `ForeignValueError(CorruptValueError)`, `FormatMismatchError(RuntimeError)`, `LayoutMismatchError(FormatMismatchError, ValueError)`.
- `sqlite_version()`, `sqlite_compile_options()`, `BUNDLED_SQLITE_VERSION`, `__version__`. Logging goes to the `diskcache_oxide` logger. There is no CLI and no environment variable to configure.

## Reading an error

```text
TypeError: values of type Episode can't be stored without pickle (only exact built-in types round-trip)
```

It is raised by `set()`, where the value was created, not by a later `get()`. Fix in this order: register the type (`register_type(Episode, name='app.Episode')`), or store a dict (a `model_dump()`), then, only if arbitrary objects are really required, open a separate `Cache(other_dir, pickle=True)` for them. A registered type that still raises has an `encode` that raised or returned something unstorable, or a field that is; that raises even with `pickle=True`. Don't add `pickle=True` to the shared cache to make the error go away: every reader of that directory then runs unpickle on whatever is written there.

```text
diskcache_oxide.Timeout: timed out after 1 s waiting for a lock or a pending fork
```

Someone held the write lock (a `clear()` in a maintenance script, a long `transact()` block, a fork in flight) for longer than `timeout`. In a request, catch it and skip the cache. If it's common, raise `timeout`, move maintenance to one scheduled job, shorten transaction blocks, or move many writing processes to a `FanoutCache`. `durable=True` turns it into a miss for single-key calls.

## Resources

- [Documentation](https://mkennedy.codes/docs/diskcache-oxide/), including [Getting started](https://mkennedy.codes/docs/diskcache-oxide/user-guide/getting-started.html), [Errors and what to do about them](https://mkennedy.codes/docs/diskcache-oxide/user-guide/errors.html), [Switching from diskcache](https://mkennedy.codes/docs/diskcache-oxide/user-guide/switching-from-diskcache.html), [Serialization](https://mkennedy.codes/docs/diskcache-oxide/user-guide/serialization.html), [NullCache](https://mkennedy.codes/docs/diskcache-oxide/user-guide/null-cache.html), [Caches](https://mkennedy.codes/docs/diskcache-oxide/user-guide/caches.html), [FanoutCache](https://mkennedy.codes/docs/diskcache-oxide/user-guide/fanoutcache.html), [Transactions](https://mkennedy.codes/docs/diskcache-oxide/user-guide/transactions.html), [Concurrency and forks](https://mkennedy.codes/docs/diskcache-oxide/user-guide/concurrency-and-forks.html), [Storage and WAL](https://mkennedy.codes/docs/diskcache-oxide/user-guide/storage-and-wal.html), [Performance](https://mkennedy.codes/docs/diskcache-oxide/user-guide/performance.html) and the [Recipes](https://mkennedy.codes/docs/diskcache-oxide/recipes/). For a user who is new to caching itself, point them at [New to caching? Start here](https://mkennedy.codes/docs/diskcache-oxide/user-guide/new-to-caching.html).
- [llms.txt](https://mkennedy.codes/docs/diskcache-oxide/llms.txt), the API index, and [llms-full.txt](https://mkennedy.codes/docs/diskcache-oxide/llms-full.txt), every signature, docstring and guide page in one file.
- [Source](https://github.com/mikeckennedy/diskcache-oxide) and [issues](https://github.com/mikeckennedy/diskcache-oxide/issues).
