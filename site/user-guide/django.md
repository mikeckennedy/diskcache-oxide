# Django cache backend

Django's cache framework runs on diskcache-oxide with one setting. `django.core.cache.cache`, the per-view and per-site cache, template fragment caching and the cache session engines all use it, and every worker process on the machine shares one cache on disk: [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) and [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) are atomic across processes, which Django's file-based cache can't promise, and there's no server to run, unlike Redis or Memcached.

``` python
CACHES = {
    'default': {
        'BACKEND': 'diskcache_oxide.DjangoCache',
        'LOCATION': '/var/lib/my-site/cache/default',
        'TIMEOUT': 300,
    },
}
```

Install it with `pip install 'diskcache-oxide[django]'`, which asks for Django 5.2 or newer. Importing `diskcache_oxide` never imports Django: the name [DjangoCache](../reference/DjangoCache.md#diskcache_oxide.DjangoCache) loads on first use. Use the dotted `BACKEND` string as above; `from diskcache_oxide import DjangoCache` works too, but where Django isn't installed it fails with Python's plain "cannot import name" instead of the install hint.

The backend runs Django's own cache tests (the `BaseCacheTests` every built-in backend passes, the middleware, template-response and i18n tests, and the cache and cached_db session tests) from each supported Django release's source, on every supported Python. Supported: Django 5.2 (the LTS), 6.0 and 6.1, on Python 3.14 and 3.14t, and Django 6.1 on 3.15 and 3.15t (5.2 and 6.0 don't support 3.15). On the free-threaded builds those tests run with the GIL staying off. They run Django single-threaded, though; the backend's own tests drive it from many threads at once, but whether Django itself is safe with many threads there is Django's question.


# The recommended setup: pages and sessions as two aliases

Most sites cache two kinds of thing. Pages, fragments and query results can be rebuilt, so a cache failure there should cost a slower page, never an error page. Sessions, rate-limit counters and job claims are state: a dropped write is a login that didn't stick, or a limit that let everyone through. Give each its own alias and directory:

``` python
CACHES = {
    'default': {
        'BACKEND': 'diskcache_oxide.DjangoCache',
        'LOCATION': '/var/lib/my-site/cache/pages',
        'TIMEOUT': 600,
        'OPTIONS': {'durable': True, 'size_limit': 2 * 1024**3},
    },
    'state': {
        'BACKEND': 'diskcache_oxide.DjangoCache',
        'LOCATION': '/var/lib/my-site/state/site',
        'TIMEOUT': None,
        'OPTIONS': {'eviction_policy': 'none'},
    },
}

SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'state'
```

`default` is durable: a write that can't get the lock in time is dropped and a read that fails is a miss (see [durable](#errors-raise-durable-drops) below). `state` isn't, so a write that fails raises, and it never evicts for size, so sessions aren't pushed out by pages; it grows until entries expire, so don't make a session for every anonymous visitor. Keep state out of the directory you'd delete on a deploy: here pages live under `cache/` and state under `state/`, as in [Sharing a cache across web workers](../recipes/web-workers.md#the-recommended-setup). The [example project](https://github.com/mikeckennedy/diskcache-oxide/tree/main/examples/django) is this setup under gunicorn.


# Settings

`LOCATION` is the cache's directory, a `str` or a `Path`, and it's required; use an absolute path (a relative one is resolved against the working directory, every time a backend instance is made). `TIMEOUT`, `KEY_PREFIX`, `VERSION` and `KEY_FUNCTION` are Django's and work as in every backend.

`OPTIONS` takes [`Cache`](getting-started.md)'s keywords by their own names: [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout), [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit), [cull_limit](../reference/Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit), [eviction_policy](../reference/Cache.eviction_policy.md#diskcache_oxide.Cache.eviction_policy), `serializer`, `pickle`, `types`, `max_connections`, `sqlite_cache_size`, `wal_size_limit`, `wal_checkpoint`, `on_decode_error`, [durable](../reference/Cache.durable.md#diskcache_oxide.Cache.durable) and `maintenance_interval`. [shards](../reference/FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) makes the alias a [`FanoutCache`](fanoutcache.md), which also takes `raise_on_mismatch`. Two defaults differ from [Cache](../reference/Cache.md#diskcache_oxide.Cache)'s:

- **[timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) is 1.0 s, not 60.** It's the lock wait: how long a write waits for another writer before raising [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout). gunicorn kills a worker after 30 s, so a 60 s wait would end in a killed worker, not an exception.
- **`pickle` is True.** Django caches any picklable object: the per-view cache stores whole `HttpResponse`s, `{% cache %}` stores `SafeString`s, and sessions and model instances are common. Values msgpack can represent (`str`, `bytes`, numbers, lists, dicts, datetimes and the rest) are still stored without pickle; only what it can't represent falls back to it.

> **Note: Two timeouts**
>
> Django's `TIMEOUT` is how long an entry lives (300 s by default, `None` for forever). `OPTIONS['timeout']` is how long a write waits for the lock. diskcache's backend called the second one `DATABASE_TIMEOUT`.

Refused with `ImproperlyConfigured`, so a setting can't silently mean something else:

- **A missing `LOCATION`, or an unknown option**, named in the message.
- **`MAX_ENTRIES` and `CULL_FREQUENCY`**, in `OPTIONS` or at the top level, in either case. diskcache-oxide bounds a cache by bytes ([size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit)) and evicts [cull_limit](../reference/Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit) entries at a time, not by an entry count. (Django's Redis and Memcached backends ignore these silently.)
- **diskcache's `SHARDS` and `DATABASE_TIMEOUT`**: use `OPTIONS['shards']` and `OPTIONS['timeout']`.
- **`OPTIONS['timeout'] = None`**, which would be the 60 s wait; leave it out for 1.0.
- **`clock`**, which would switch background maintenance off for the directory.
- **Two aliases on one `LOCATION` with different `OPTIONS`.** They share one cache (see below), so the first one's settings would otherwise win silently. Django's settings (`TIMEOUT`, `KEY_PREFIX`, `VERSION`, `KEY_FUNCTION`) may differ.
- **A durable alias that the [cache](../reference/DjangoCache.cache.md#diskcache_oxide.DjangoCache.cache) or `cached_db` session engine uses**, or a subclass of either engine or of the backend. See [sessions](#sessions) below.

`pickle: False` is allowed but narrow: only the low-level API with plain types works. Template fragments, the per-view and per-site cache and model instances raise `TypeError`, since what they store can't be represented without pickle. As with any Django cache, a cache that pickles must be writable only by the app: the backend creates its directory `0700`, and one you made yourself keeps the mode you gave it.


# Errors raise; durable drops

By default the backend raises, as a Redis backend raises on a lost connection: a write that can't get the lock within [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) (a `TimeoutError`), and a directory that can't be opened raises on first use. Django's cache also holds sessions and rate limits, where a silent drop is a bug.

`OPTIONS = {'durable': True}` is for a page cache that must never fail a request. Then a single-key write that can't be done is dropped instead ([add](../reference/Cache.add.md#diskcache_oxide.Cache.add), [touch](../reference/Cache.touch.md#diskcache_oxide.Cache.touch) and [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete) return False, [set](../reference/Cache.set.md#diskcache_oxide.Cache.set) returns None as always, [set_many](../reference/DjangoCache.set_many.md#diskcache_oxide.DjangoCache.set_many) returns the keys it didn't store), and a read that fails is a miss. A batch behind a held lock waits out the lock wait once, not once per key. Durable doesn't cover everything:

- [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) and [decr](../reference/Cache.decr.md#diskcache_oxide.Cache.decr) raise Django's `ValueError`, as for a missing key, since the engine reports an outage there as a miss.
- [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout).
- If the directory can't be opened at all, the backend serves a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) until the next try (where every read misses, nothing is stored, and [add](../reference/Cache.add.md#diskcache_oxide.Cache.add) always succeeds), as the [`Caches` registry](caches.md) does: after 1 s for a [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), logged as a WARNING on the `diskcache_oxide` logger, and after 10 s for any other failure to open, logged as an ERROR with the traceback. That includes a directory holding the other kind of cache ([shards](../reference/FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) set on a plain cache's directory, or left out on a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache)'s), which never heals by itself, so watch for that ERROR. Bad settings still raise.

Because [add](../reference/Cache.add.md#diskcache_oxide.Cache.add) succeeds on a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) and returns False behind a held lock, code that uses [add](../reference/Cache.add.md#diskcache_oxide.Cache.add) as a lock or a once-only claim belongs on a plain alias. Django's own callers are fine: `get_or_set` works either way, and sessions can't use a durable alias.


## Sessions

A durable alias for the [cache](../reference/DjangoCache.cache.md#diskcache_oxide.DjangoCache.cache) or `cached_db` session engine is refused. The cache engine reads a dropped write as "that session key is taken" and tries again, up to 10,000 times; the cached_db engine never learns of a dropped cache write, so later requests read the old session from the cache. The check happens when the alias's backend is first built: under `manage.py` that's at startup, since Django's system checks build every alias, and under a bare gunicorn it's the first request that uses the alias (or startup, if the app touches the cache at import, as the example's `wsgi.py` does).

With `cached_db` sessions on a plain alias, a held write lock still costs something: reads succeed while writes wait. A `save()` gives up after the lock wait (Django only logs it), so the next request may read the previous session from the cache while the database has the new one; the next save fixes that. A logout can raise [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) after the database row is gone, and then the old cookie still loads the session from the cache: the visitor stays logged in, and a request that changes the session gets Django's 400 (`SessionInterrupted`), until they log out again or the cache entry expires. This is how `cached_db` behaves on any cache that raises.


# One cache per directory, every worker sharing it

Django builds a backend instance per thread, and under ASGI up to one per request. None of them owns a cache: every instance for a directory, in every thread of a process, shares one [Cache](../reference/Cache.md#diskcache_oxide.Cache), opened on its first use and kept for the life of the process, so making an instance opens nothing. Every process that opens the same directory shares the same cache on disk. Opening before or after a `fork()` (gunicorn's `preload_app`, Celery's prefork pool) is fine: see [Concurrency and forks](concurrency-and-forks.md).

- **[close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) does nothing.** Django calls it after every request; closing would reopen the cache on the next one. The handles live for the process, and background maintenance keeps the cache tidy.
- **[clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) clears the directory**: every alias, prefix and version on that `LOCATION`, as Django's file and Redis backends do. Aliases that must clear separately need separate `LOCATION`s.
- **Never delete the directory under running workers.** They would carry on in the deleted files, and a worker started later would get a new, empty cache the others never see. Use `cache.clear()`.
- **When to use [shards](../reference/FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards).** A plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) suits most sites. Make the alias that many worker processes write to most a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) with `OPTIONS = {'shards': 8}`: with 8 worker processes at 99 reads to 1 write it's about 4x a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) ([FanoutCache](fanoutcache.md) has the numbers). Choose it before the directory is made: a directory holds one kind or the other.


# The calls

Every call goes through Django's [make_and_validate_key](../reference/DjangoCache.make_and_validate_key.md#diskcache_oxide.DjangoCache.make_and_validate_key), so `KEY_PREFIX`, `VERSION` and `KEY_FUNCTION` work, and Django's `CacheKeyWarning` for keys memcached couldn't store still appears (keys are checked in under 0.1 µs when they're fine). A `KEY_FUNCTION` must return a `str`.

- **[get](../reference/Cache.get.md#diskcache_oxide.Cache.get), [set](../reference/Cache.set.md#diskcache_oxide.Cache.set), [add](../reference/Cache.add.md#diskcache_oxide.Cache.add), [touch](../reference/Cache.touch.md#diskcache_oxide.Cache.touch), [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete), [has_key](../reference/DjangoCache.has_key.md#diskcache_oxide.DjangoCache.has_key)**: one call each. A timeout of 0 or less stores nothing (and removes the key if it was there), `None` is forever. A timeout that isn't a number (a `Decimal`, a `str`, a `bool`) raises `TypeError`.
- **[incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) and [decr](../reference/Cache.decr.md#diskcache_oxide.Cache.decr)** are atomic across threads and processes and keep the entry's expiry. A missing key raises `ValueError`; a value that isn't a number (an `int` or a `float`; a stored `bool` doesn't count) raises `TypeError`.
- **[get_many](../reference/DjangoCache.get_many.md#diskcache_oxide.DjangoCache.get_many)** returns a stored `None`; **[set_many](../reference/DjangoCache.set_many.md#diskcache_oxide.DjangoCache.set_many)** is a [set](../reference/Cache.set.md#diskcache_oxide.Cache.set) per key, so no pickling code runs while the cache's write lock is held; **[delete_many](../reference/DjangoCache.delete_many.md#diskcache_oxide.DjangoCache.delete_many)** deletes up to 100 keys per transaction (per shard on a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache)), validating every key first.
- **The async methods** ([aget](../reference/DjangoCache.aget.md#diskcache_oxide.DjangoCache.aget), [aset](../reference/DjangoCache.aset.md#diskcache_oxide.DjangoCache.aset), [aincr](../reference/DjangoCache.aincr.md#diskcache_oxide.DjangoCache.aincr) and the rest) each run their sync form in one call through asgiref's `sync_to_async`, as Django does, so [aincr](../reference/DjangoCache.aincr.md#diskcache_oxide.DjangoCache.aincr) is atomic and keeps the expiry on every supported Django line (Django 5.2's and 6.0's own [aincr](../reference/DjangoCache.aincr.md#diskcache_oxide.DjangoCache.aincr) is a get and then a set).

`get_or_set`, `incr_version`, `decr_version` and their async forms are Django's own, built from the calls above. Beyond Django's API: `tag=` on [set](../reference/Cache.set.md#diskcache_oxide.Cache.set) and [add](../reference/Cache.add.md#diskcache_oxide.Cache.add) (and [aset](../reference/DjangoCache.aset.md#diskcache_oxide.DjangoCache.aset), [aadd](../reference/DjangoCache.aadd.md#diskcache_oxide.DjangoCache.aadd)), then `evict(tag)` removes every entry with that tag; `pop(key)`; [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) and [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull); [stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats), [maintenance_stats()](../reference/Cache.maintenance_stats.md#diskcache_oxide.Cache.maintenance_stats) and [check()](../reference/Cache.check.md#diskcache_oxide.Cache.check); and [cache](../reference/DjangoCache.cache.md#diskcache_oxide.DjangoCache.cache), the [Cache](../reference/Cache.md#diskcache_oxide.Cache) or [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) underneath, whose keys are the made keys (`prefix:version:key`).

``` python
from django.core.cache import cache

cache.set(f'episode:{n}:html', html, timeout=600, tag=f'episode:{n}')
cache.set(f'episode:{n}:summary', summary, timeout=600, tag=f'episode:{n}')
cache.evict(f'episode:{n}')   # after an edit: both entries go
```

> **Warning: A cached view that reads the session**
>
> `@cache_page` stores the response before `SessionMiddleware` adds `Vary: Cookie`, so a cached view that reads the session serves one visitor's page to everyone. Add `@vary_on_cookie` to such a view, or keep the session out of it. This is Django's behavior on every backend.


# In tests

`override_settings(CACHES=...)` works: when `CACHES` changes, the backend closes every handle it opened, so a test suite that gives each test its own temporary `LOCATION` doesn't run out of file descriptors, and a test that deletes its directory doesn't leave a handle on deleted files. To release them yourself (before deleting a directory a test made, say), call `diskcache_oxide.djangocache.close_all()`; the next call reopens. On Windows, a directory can't be deleted while a handle on it is open, so this is what makes a Django-style teardown work there.


# Coming from diskcache's DjangoCache

Change `'BACKEND': 'diskcache.DjangoCache'` to `'BACKEND': 'diskcache_oxide.DjangoCache'`, then the settings: `SHARDS` becomes `OPTIONS['shards']` (diskcache's backend is always a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache), 8 shards by default; this one is a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) unless you ask), `DATABASE_TIMEOUT` becomes `OPTIONS['timeout']`, and `MAX_ENTRIES` goes (use [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit)). The cache starts cold: diskcache-oxide doesn't read diskcache's files. diskcache's `read=`, `deque()`, `index()`, `cache(name)`, [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) and tag-index calls aren't here; its 10 ms timeout with silent retries, which wait for as long as the lock is held, is replaced by errors that raise, or [durable](../reference/Cache.durable.md#diskcache_oxide.Cache.durable). [Divergences](divergences.md) has the full list under D-062.
