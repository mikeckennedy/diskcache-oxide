# Recipes

Practical patterns for the jobs a shared cache does in a real app. The examples marked as runnable execute against diskcache-oxide every time these docs are built, so they're known to work.


# Caching

**[Caching expensive calls](caching-expensive-calls.md).** The cache-aside pattern, and a small decorator that turns any function into a cached one, with keys built safely from its arguments.

**[Invalidating groups of keys](invalidating-groups.md).** Clear everything cached for one user, one product or one page at once: delete by key prefix, or bump a generation number so stale entries are never read again.


# Coordinating workers

**[Running a job once across workers](run-once.md).** Eight gunicorn workers each schedule the same hourly job, and you want it to run once. [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) is atomic across processes, so exactly one of them claims it.

**[Rate limiting](rate-limiting.md).** Count requests per client in fixed time windows with [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) and [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr), shared by every worker on the machine, with no Redis.


# Deployment and testing

**[Sharing a cache across web workers](web-workers.md).** Where to put the cache directory, when to open it with gunicorn, uvicorn, FastAPI and Flask, how to keep a busy cache from slowing requests, and what to do at shutdown.

**[Testing code that uses a cache](testing.md).** pytest fixtures for a real cache in a temporary directory, a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) for tests that shouldn't cache at all, and a fake clock for testing expiry without sleeping.


# Coming later

More recipes will arrive with the features they need: `FanoutCache` for write-heavy threaded apps, `memoize` with stampede protection, tags, [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact), cross-process locks and throttles, and `AsyncCache` for asyncio apps. See the [roadmap](../user-guide/roadmap.md).
