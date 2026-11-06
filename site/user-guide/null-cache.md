# NullCache

Sooner or later you need to turn caching off. In tests, so each one starts clean. In local development, so you see your change instead of yesterday's. In production, at the worst possible moment, while you chase a bug that might be a stale entry and might not. Without help, that means `if cache is not None:` around every call, or a hand-written fake that drifts a little further from the real API with every release.

[NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) is a cache that's switched off. It has the same methods and signatures as [Cache](../reference/Cache.md#diskcache_oxide.Cache), so swapping one for the other is a one-line change, and the code that uses it never knows.

``` python
from diskcache_oxide import Cache, CacheLike, NullCache

def make_cache(settings) -> CacheLike:
    if settings.cache_enabled:
        return Cache(settings.cache_dir)
    return NullCache()
```


# One rule

> **A [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) behaves like a real cache in which every entry expires the instant it's written and is swept straight away.**

Every row of this table follows from that sentence, so you never have to guess, and once it clicks you never have to look it up again:

| Call | Result | Because |
|----|----|----|
| `get(key, default)` | `default` | the entry already expired |
| `cache[key]` | raises `KeyError` | the entry already expired |
| `set(...)` | `True` | the write succeeded, then expired |
| `add(...)` | `True` | the key was absent, so the add succeeded |
| `incr(key, delta, default)` | `default + delta` | it starts from the default each time; `KeyError` if `default=None` |
| [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete), [touch](../reference/Cache.touch.md#diskcache_oxide.Cache.touch), `key in cache` | `False` | there's nothing there |
| `pop(key, default)` | `default` | there's nothing there |
| `len(cache)`, `cache.volume()` | `0` | it's empty and uses no disk |
| [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire), [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) | `0` | nothing to remove |
| iteration, [iterkeys()](../reference/Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys) | nothing | it's empty |
| `bool(cache)`, `cache.enabled` | `False` | caching is off |

The constructor accepts the same arguments as [Cache](../reference/Cache.md#diskcache_oxide.Cache) and ignores them, and the directory is optional. Bad argument *types* still raise as they would on [Cache](../reference/Cache.md#diskcache_oxide.Cache) (a non-`str` key, `expire=True`), so code that passes with a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) in tests also works with a real one.


## Watch [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add)

The rule means [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) always succeeds on a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache). If you use [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) to claim a job so only one worker runs it, then with caching off every worker's claim succeeds and the job runs once per worker. That's harmless for idempotent work and wrong for work with side effects, like sending email. Keep claims like that on a cache that's never switched off.

`Cache(durable=True)` goes the other way: its [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) returns `False` when a write fails, so a claim fails closed there and open here. Keep claims on a handle that isn't durable and handle its exceptions. See [A cache that can't take a request down](getting-started.md#a-cache-that-cant-take-a-request-down).


# `if cache:` means "caching is on"

`bool(Cache(...))` is always True and `bool(NullCache())` is always False, and both have an [enabled](../reference/Cache.enabled.md#diskcache_oxide.Cache.enabled) property. (In diskcache, an empty real cache is falsy, which makes this check unreliable.)

``` python
if cache:
    log.info('Caching is on')
```


# Keep invalidating while caching is off: `invalidate=`

Turning caching off creates a trap. While it's off, your app keeps changing data, and the code that would have deleted the now-stale cache entries is talking to a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache). When you turn caching back on, the real cache serves the stale values.

`invalidate=` closes the trap. Give the [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) the real cache, and it forwards the operations that *remove* data to it, while everything else stays switched off:

``` python
real = Cache('/var/cache/my-app')
cache = NullCache(invalidate=real)

cache.set('user:42', profile)    # not cached
cache.get('user:42')             # None
cache.delete('user:42')          # deleted from the real cache
cache.pop('user:42')             # deleted from the real cache; returns the default
cache.clear()                    # clears the real cache
cache.iterkeys(prefix='user:')   # the real cache's keys, so list-then-delete still works
```

When caching comes back on, nothing you invalidated by removing it is waiting.

Only removals are forwarded, so invalidating by *writing* is lost while caching is off. Overwriting the stale value with a fresh [set](../reference/Cache.set.md#diskcache_oxide.Cache.set), or bumping a version key with [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) so readers build a new key, does nothing, and the old value is served once caching is back on:

``` python
cache = NullCache(invalidate=real)   # real holds 'user:42' = old_profile, and 'users:version' = 3

cache.set('user:42', new_profile)    # dropped: real still has old_profile
cache.incr('users:version')          # dropped: still 3, so readers keep reading the old keys
```

If your app invalidates that way, delete the entry instead (or as well), or [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) the real cache when you turn caching back on.


# [CacheLike](../reference/CacheLike.md#diskcache_oxide.CacheLike)

[CacheLike](../reference/CacheLike.md#diskcache_oxide.CacheLike) is a `typing.Protocol` that both [Cache](../reference/Cache.md#diskcache_oxide.Cache) and [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) satisfy. Annotate with it and ty or pyright will accept either:

``` python
from diskcache_oxide import CacheLike

def load_user(user_id: str, cache: CacheLike) -> dict:
    if (user := cache.get(f'user:{user_id}')) is not None:
        return user
    user = db.load_user(user_id)
    cache.set(f'user:{user_id}', user, expire=300)
    return user
```

A test in diskcache-oxide's own suite checks that [Cache](../reference/Cache.md#diskcache_oxide.Cache), [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) and [CacheLike](../reference/CacheLike.md#diskcache_oxide.CacheLike) keep identical public signatures, so they can't drift apart.


# [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) and [TransactionalCacheLike](../reference/TransactionalCacheLike.md#diskcache_oxide.TransactionalCacheLike)

[NullCache.transact()](../reference/NullCache.transact.md#diskcache_oxide.NullCache.transact) is a no-op context manager that yields the [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache), so code written as `with cache.transact() as tx:` runs unchanged with caching off. There's nothing to commit or roll back. It accepts `retry=` as [Cache.transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) does, and a keyword-only `key=`, which it ignores.

With `invalidate=`, it never opens a transaction on the real cache: a switched-off cache must never take a write lock or raise [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout). So removals forwarded inside the block land at once, and an exception in the block doesn't undo them.

[CacheLike](../reference/CacheLike.md#diskcache_oxide.CacheLike) has no [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact). To accept either class and call it, annotate with [TransactionalCacheLike](../reference/TransactionalCacheLike.md#diskcache_oxide.TransactionalCacheLike), which adds it. See [Transactions](transactions.md#typing-transactionalcachelike). A [`FanoutCache`](fanoutcache.md)'s [transact](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) takes a key, so for code that accepts a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) or a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) use [ShardedCacheLike](../reference/ShardedCacheLike.md#diskcache_oxide.ShardedCacheLike) instead (see [Typing](fanoutcache.md#typing)); the `key=` a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) ignores is what lets it satisfy both.
