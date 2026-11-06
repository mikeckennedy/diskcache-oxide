# Sharing a cache across web workers

The usual deployment: several worker processes, each opening the same cache directory, all sharing one cache. diskcache-oxide is built for exactly this, so there isn't much to it. This page covers the few choices that matter.


# Pick a directory

- **Somewhere persistent and private:** `/var/cache/<app>`, owned by the user the app runs as. diskcache-oxide creates the directory `0700` if it doesn't exist; if you create it yourself, keep it that way. Anyone who can write to a cache directory can change what your app reads from it.
- **On a local disk.** The cache relies on SQLite's write-ahead log and shared memory, which don't work on network filesystems like NFS. diskcache-oxide checks this when it opens the cache and fails loudly instead of risking corruption.
- **In Docker,** mount a volume for it if the cache should survive container restarts, and give every worker container on a host the same volume if they should share it.
- **One directory per purpose.** Separate directories for page fragments, API responses, and coordination keys (job claims, rate limits) keep their size limits and eviction independent.


# Open it once per process

Open the cache once, at startup, and share it across requests and threads. A [Cache](../reference/Cache.md#diskcache_oxide.Cache) is safe to use from any number of threads.


## FastAPI

``` python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from diskcache_oxide import Cache

cache = Cache('/var/cache/my-app', timeout=1.0)

@asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    cache.checkpoint()
    cache.close()

app = FastAPI(lifespan=lifespan)
```


## Flask

``` python
import atexit

from flask import Flask

from diskcache_oxide import Cache

app = Flask(__name__)
cache = Cache('/var/cache/my-app', timeout=1.0)

@atexit.register
def close_cache() -> None:
    cache.checkpoint()
    cache.close()
```


## Pre-forking servers

With gunicorn (including `--preload`), uvicorn `--workers`, or Celery's prefork pool, it doesn't matter whether you open the cache before or after the fork. diskcache-oxide's fork barrier makes sure no worker starts life holding half a database transaction, and each child opens its own fresh connections on first use. See [Concurrency and forks](../user-guide/concurrency-and-forks.md).


# Keep the request path fast

Reads never wait for writers, so a [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) stays fast however busy the cache is. Writes are different: only one process can commit to a cache file at a time, and a write waits up to the cache's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) for its turn (60 seconds by default, matching diskcache). In a web request you'd rather skip a cache write than make the user wait, so give the cache a short timeout and treat [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) as "don't cache this time":

``` python
from diskcache_oxide import Timeout

def cache_quietly(key: str, value: object, expire: float) -> None:
    try:
        cache.set(key, value, expire=expire)
    except Timeout:
        log.warning('cache busy; skipped storing %s', key)
```

Under normal load a write takes about 25 µs and never comes near the timeout. The timeout matters during bursts, such as a big [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) from a maintenance script, and even then [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) steps aside between chunks to let waiting writes in.


# Maintenance

Run maintenance from one place, such as a scheduled job (see [Running a job once across workers](run-once.md)), not from every worker:

- `cache.expire()` removes expired entries and frees their space. A cache that's mostly read can accumulate expired entries, because the cleanup each [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) does is small. Once an hour is plenty for most apps.
- `cache.checkpoint()` empties the write-ahead log after a write burst. It returns `False` instead of waiting long if workers are busy, so it's safe to call any time.


# At shutdown

Calling [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) and [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) at shutdown, as in the examples above, is optional but tidy. [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) copies the write-ahead log into the database and truncates it, so a large log left by a burst of writes doesn't sit on disk. [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) does the same automatically when the log is large, waiting at most a second. See [Storage and WAL](../user-guide/storage-and-wal.md).


# Switching caching off

To run with caching off (local development, or chasing a stale-data bug in production), build the cache in one factory function and return a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) when it's disabled. Nothing else in the app changes. Pass `invalidate=` so deletes still reach the real cache while it's off. See [NullCache](../user-guide/null-cache.md).

``` python
from diskcache_oxide import Cache, CacheLike, NullCache

def make_cache(settings) -> CacheLike:
    real = Cache(settings.cache_dir, timeout=1.0)
    return real if settings.cache_enabled else NullCache(invalidate=real)
```
