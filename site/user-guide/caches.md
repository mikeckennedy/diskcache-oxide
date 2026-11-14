# Caches: named caches

Every app with two caches eventually grows a file called something like `cache_service.py`. It maps names to directories, opens each cache once and hands out the same object afterwards, carries on with caching switched off when a directory can't be opened, lets you turn one off for a load test, and feeds counts and sizes to an admin page. It's the same file in every app, written slightly differently each time. [Caches](../reference/Caches.md#diskcache_oxide.Caches) is that file, so you don't have to write it again.

``` python
from diskcache_oxide import Caches

caches = Caches('/var/lib/my-app/cache', durable=True, timeout=1.0)
caches.register('general')
caches.register('pages', size_limit=2 * 2**30)

caches['general'].set('greeting', 'hello')
caches['pages'].get('/about')
```

Settings passed to [Caches](../reference/Caches.md#diskcache_oxide.Caches) apply to every cache, and [register](../reference/Caches.register.md#diskcache_oxide.Caches.register) can override them for one. Each cache lives in a directory named after it under the base directory, unless you pass `directory=`. The base directory holds caches; it isn't one itself, so don't open a [Cache](../reference/Cache.md#diskcache_oxide.Cache) on it.

A registry is for caches: data you can rebuild, where an outage should mean a miss. That's why the examples here pass `durable=True` and a short [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout). Sessions, job claims, locks and work queues are state, and they don't belong in a registry: see [State isn't cache](#state-isnt-cache) below.

One setting works differently here: a function passed as `on_decode_error` is called as `report(name, key, exc)`, with the cache's name first, so one reporter can serve every cache. (On a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) it's `report(key, exc)`.) A function that doesn't take three arguments is rejected when you create the registry or register the cache.


# Lookups

A cache opens on its first lookup, and every lookup after that returns the same object. A lookup is a single dict read with no lock, so it's fine to do on every request rather than keeping your own references. Lookups are safe from any thread, on free-threaded Python too, and in a child process after a fork.

[Caches](../reference/Caches.md#diskcache_oxide.Caches) is generic in its names. Give it a `Literal` and a misspelled name becomes a type error:

``` python
from typing import Literal

CacheName = Literal['general', 'pages']
caches: Caches[CacheName] = Caches('/var/lib/my-app/cache')
caches['genral']  # ty: invalid argument
```


# When a cache can't open

A cache can fail to open: a permissions problem, a disk that went away, a directory that holds the other kind of cache, or another process holding the lock for longer than [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout). (A cache in another on-disk format isn't one of them: it opens in its versioned subdirectory, below.) A lookup then returns a [NullCache](null-cache.md) instead of raising, so the request carries on without the cache. The error goes to `on_open_error(name, exc)`, which logs it at `ERROR` on the `diskcache_oxide` logger by default. Pass your own function to send it to Sentry or your logging setup.

The failure is remembered for `retry_open_after` seconds (10 by default), so a broken cache costs one failed open every 10 seconds, not one per request, and the lookup after that tries again. A cache that recovers is picked up without a restart. Under load, several threads may make that retry at the same moment, and each one that fails reports it.

`MemoryError` isn't an open failure, so it raises from the lookup, as it would from `Cache(...)`. That includes SQLite running out of memory while opening (`SQLITE_NOMEM`), which used to be an `OSError` and got a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache).

A [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) at open means busy, not broken: another process holding the lock, or a first lookup that collided with a fork. It's remembered for at most 1 second, and the default report is a `WARNING` with no traceback. A custom `on_open_error` still gets the exception. Lookups of a cache that's already open never touch any of this.

A bad *setting* (`size_limit=True`, `serializer='yaml'`) is a bug, not an outage, so it raises on lookup instead. What each open error means, and the fix, is in [Errors and what to do about them](errors.md).


## After an upgrade that changes the format

When a release changes the on-disk format, a registered cache whose directory holds the old format opens a new, empty cache in the subdirectory `diskcache-oxide-format-N` and logs a warning, as a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) does: no [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache), no retry, and `on_open_error` isn't called. `caches.directory(name)` still gives the registered directory, and the handle's [directory](../reference/Cache.directory.md#diskcache_oxide.Cache.directory) says where the cache really is. Once the old workers are gone, `caches.remove_old_formats()` removes every registered cache's old files and returns what it removed, by name. It doesn't check for old workers: read [Upgrading across a format change](../recipes/upgrading.md) before you call it.


# Sharded caches

Pass `shards=` to [register](../reference/Caches.register.md#diskcache_oxide.Caches.register) to open a [`FanoutCache`](fanoutcache.md) instead of a [Cache](../reference/Cache.md#diskcache_oxide.Cache). It takes the same settings, and `max_connections` is then a total across the shards.

``` python
caches.register('rate_limits', shards=8)
caches.fanout('rate_limits').incr('ip:203.0.113.7')
```

A plain cache is read with `caches[name]` and a sharded one with `caches.fanout(name)`, typed `FanoutCache | NullCache`, because its [transact](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) takes a key. Reading a name the wrong way raises `TypeError`. Passing [shards](../reference/FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) at all picks the kind, even `shards=None`, so a config file that writes `shards: null` for "none" would register a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache): leave it out. A directory that holds the other kind of cache fails to open with [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError), and the lookup returns a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) like any failed open.

A sharded cache keeps the shard count it was created with. Every process then routes keys by the same count, so a deploy that changes `shards=` keeps working while old and new workers overlap. When the folder already has another count than the registration:

- The cache opens with the count on disk. The registry logs a `WARNING` saying so and how to apply the new count, and one `ERROR` per process so an error tracker hears of it.
- To apply the new count, delete the folder with every worker stopped, for example at deploy. A folder deleted while old workers still run is re-created by whichever process opens it first, with that process's count.
- A [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) or `max_connections` that fits the registered count but can't be split over the folder's (each shard needs at least one connection and 1 MiB) fails with [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError) naming both counts, and the lookup returns a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache). A setting that's wrong for the registered count itself still raises `ValueError` from the lookup, as any bad setting does.
- `register(..., raise_on_mismatch=True)` makes any other count an open failure instead, so the lookup returns a reported [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache). A direct `FanoutCache(path, shards=16)` behaves the same way: the stored count with a `WARNING`, or [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError) with `raise_on_mismatch=True`.

``` python
caches.register('rate_limits', shards=16)  # the folder has 8: opens with 8, and warns
caches.fanout('rate_limits').shards        # 8
```

Everything else on this page ([disable](../reference/Caches.disable.md#diskcache_oxide.Caches.disable), [stats](../reference/Cache.stats.md#diskcache_oxide.Cache.stats), [clear](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), [expire_all](../reference/Caches.expire_all.md#diskcache_oxide.Caches.expire_all), [close_all](../reference/Caches.close_all.md#diskcache_oxide.Caches.close_all)) works the same for both kinds.


# Switching caches off

``` python
caches.disable('pages')   # later lookups return NullCache(invalidate=real)
caches.enable('pages')
caches.enabled('pages')   # False while it's off
```

While a cache is disabled, its lookups return `NullCache(invalidate=real)`: reads miss and writes are dropped, but [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete) and [clear](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) still reach the real cache, so entries you invalidate by removing them aren't served stale when it comes back on. Invalidating by writing doesn't survive: an overwriting [set](../reference/Cache.set.md#diskcache_oxide.Cache.set) or a version-bump [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) is dropped like any other write, and the old value is served again (see [Keep invalidating while caching is off](null-cache.md#keep-invalidating-while-caching-is-off-invalidate)). Switches only change later lookups. Code that already holds the real [Cache](../reference/Cache.md#diskcache_oxide.Cache) keeps using it, so set them at startup, before you hand caches out.

If your app has its own names for what it caches (Talk Python calls them purposes), map them onto registered caches in your own code. It's a few lines:

``` python
PURPOSES = {'rss_feed': 'general', 'episode_html': 'episodes'}
off = {'rss_feed'}  # from your settings file

def cache_for(purpose: str) -> CacheLike:
    real = caches[PURPOSES[purpose]]
    return NullCache(invalidate=real) if purpose in off else real
```

Build the wrapper on each call rather than memoizing it: `caches[...]` is already cheap, and a memoized `NullCache(invalidate=...)` wrapped around a cache that had failed to open would never pick up its recovery.


# [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) on a registered cache

`@caches['episodes'].memoize()` does the lookup once, when the module is imported, and the decorated function keeps whatever came back. If the cache couldn't open at that moment, the function holds a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) and runs uncached until the process restarts, even after the cache recovers. `disable('episodes')` never reaches it either, because switches only change later lookups.

If that matters to you, look the cache up on each call instead. A small decorator does it:

``` python
import functools

def memoized(name: str, **options):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            return caches[name].memoize(**options)(func)(*args, **kwargs)
        return wrapper
    return decorator

@memoized('episodes', expire=600)
def episode(number: int) -> dict:
    return load_episode(number)
```

It stores under the same keys as `@caches['episodes'].memoize(expire=600)` would, and it follows recovery and the switches. Building the wrapper on every call added about 2 µs per call when we measured it. To forget one result, build the key the same way: `caches['episodes'].delete(caches['episodes'].memoize()(episode.__wrapped__).__cache_key__(42))`.


# State isn't cache

Sessions, job claims, locks and work queues look like cache entries, but losing one isn't a miss: it's a forced logout, a job that runs twice, a lock two workers hold at once, a job that vanishes. Open each of them as a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache), directly, not through the registry:

``` python
from pathlib import Path

from diskcache_oxide import Cache

STATE = Path('/var/lib/my-app/state')
sessions = Cache(STATE / 'sessions', timeout=3.0, eviction_policy='none')
coordination = Cache(STATE / 'coordination', timeout=3.0, eviction_policy='none')
jobs = Cache(STATE / 'jobs', timeout=3.0, eviction_policy='none')
```

Why not the registry: a registry hands out a [`NullCache`](null-cache.md) whenever a cache can't open, and while one is disabled, whatever [durable](../reference/Cache.durable.md#diskcache_oxide.Cache.durable) says. On a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache):

- [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) always returns `True`, so every worker wins a job claim and the job runs once per worker.
- [push()](../reference/Cache.push.md#diskcache_oxide.Cache.push) returns a key and stores nothing, so a queued job is gone.
- [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) returns `True`, so a login "succeeds" and the next request is logged out.
- The [coordination recipes](coordination.md) ([Lock](../reference/Lock.md#diskcache_oxide.Lock), [RLock](../reference/RLock.md#diskcache_oxide.RLock), [BoundedSemaphore](../reference/BoundedSemaphore.md#diskcache_oxide.BoundedSemaphore), [throttle](../reference/throttle.md#diskcache_oxide.throttle), [barrier](../reference/barrier.md#diskcache_oxide.barrier), [Averager](../reference/Averager.md#diskcache_oxide.Averager)) raise `TypeError`, on purpose: a lock that every caller holds at once is worse than an error.

Why not `durable=True`: a durable handle drops a write it can't make and reports it later. For a claim that means [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) returns `False`, so nobody runs the job; for a session, a login that silently didn't happen. The coordination recipes refuse a durable cache with `TypeError` for the same reason. Without [durable](../reference/Cache.durable.md#diskcache_oxide.Cache.durable), the failure raises ([Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), `OSError`) and the app decides: a 503 page in a request, a retry in a worker.

Why `eviction_policy='none'`: eviction removes the least recently stored entries first, and a long-lived session or a claim that's still live is exactly that. With eviction off, an entry goes only when it expires or you delete it, so give claims and locks an [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire).

The registry's directory check can't see caches you open yourself, so keep state out of the registry's base directory: a later `register('sessions')` would otherwise share a store with the one you opened. The [recommended setup](../recipes/web-workers.md#the-recommended-setup) puts state in a sibling `state/` directory for that reason.

The [queue calls](../recipes/work-queues.md) are on [Cache](../reference/Cache.md#diskcache_oxide.Cache) only, so a queue's cache is never a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache).


# Admin and shutdown

``` python
caches.stats()                      # {name: CacheStats(name, directory, enabled, count, volume, error)}
caches.clear('pages', timeout=10)   # works on a disabled cache too
caches.expire_all(timeout=10)       # {name: removed, or None if it failed}
caches.close_all()                  # at shutdown; `with Caches(...) as caches:` does it too
```

- [stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats) opens every registered cache and never raises: a cache that can't be read has its `error` set.
- Hit and miss counts come from each cache's own [stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats): `caches['pages'].stats()` turns counting on for this process, and later calls return `(hits, misses)` for this process's lookups. A disabled cache, or one that couldn't open, returns `(0, 0)`.
- `clear(name)` raises if the cache can't be opened, so an admin page doesn't report "0 cleared" for a failure.
- [expire_all()](../reference/Caches.expire_all.md#diskcache_oxide.Caches.expire_all) runs every cache, logging and reporting `None` for one that fails. Run it from a scheduled job rather than inside a request: a large expire takes seconds.
- Maintenance calls hold the write lock once per chunk, so give them a longer `timeout=` than the request path's.


# Coming from your own diskcache wrapper

Apps that run diskcache in production tend to grow the same wrapper around it: a class that turns errors into misses, a null cache for turning caching off, and a module that creates, lists and maintains the app's caches. Talk Python's, for one, are a `DurableCache` class and a `cache_service` module, and the left column uses their names. Each piece now has a home in the library:

| A typical wrapper | diskcache-oxide |
|----|----|
| `bool()` is always True | [Cache](../reference/Cache.md#diskcache_oxide.Cache) is always truthy (D-009); [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) is falsy |
| `CacheProtocol` | [CacheLike](../reference/CacheLike.md#diskcache_oxide.CacheLike) |
| [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache), `PerfTestNullCache` | [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache), and `NullCache(invalidate=real)` |
| [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) that logs, deletes and misses on any error | `Cache(on_decode_error='miss')` or a reporting function ([details](getting-started.md#values-that-cant-be-read)) |
| A cache that can't take a request down | `Caches(base_dir, durable=True)`, which also covers [set](../reference/Cache.set.md#diskcache_oxide.Cache.set), whose failures such a wrapper usually raises ([details](getting-started.md#a-cache-that-cant-take-a-request-down)) |
| `create_cache(name, size_limit)` | `caches.register(name, size_limit=...)`, then `caches[name]` |
| `timeout=1.0` on every open | `Caches(base_dir, timeout=1.0)` |
| A [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) when opening fails | the same, reported through `on_open_error` and retried after 10 s |
| `get_all_caches()`, `clear_*()`, `close_all_caches()` | `caches.stats()`, `caches.clear(name, timeout=...)`, `caches.close_all()` |
| A separate maintenance handle with a 10 s timeout | `timeout=` on [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) and [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) |
| Hourly `expire_all_caches()` inside `create_cache` | `caches.expire_all()` from a scheduled job |

The library's self-healing read is safer than the wrapper's in three ways:

- **It deletes only what failed.** The wrapper's [get](../reference/Cache.get.md#diskcache_oxide.Cache.get)-then-[delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete) can remove a fresh value another process wrote in between. The library deletes exactly the stored version it couldn't read.
- **It never waits to clean up.** If the write lock is busy, the bad row stays and a later read tries again.
- **It doesn't hide outages.** The wrapper catches every exception, so a lock [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) also became a silent miss. In the library, [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), I/O errors and `MemoryError` still raise by default, and so does reading a pickled value from a cache without `pickle=True`, which is a configuration mistake. `durable=True` is how you choose a miss for an outage, and it counts each one and logs them, at most once a minute, rather than staying silent; `MemoryError` and that mistake raise regardless.

What stays in the app: reading its settings file and environment variables (pass the result as `base_dir`), its purposes and their on/off flags, descriptions for the admin page, and wiring the reporting functions to Sentry or logbook.
