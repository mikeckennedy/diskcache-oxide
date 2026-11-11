# Recipes

A shared cache does more jobs in a real app than caching. It elects a worker, counts requests, holds sessions and one-time tokens, remembers what was invalidated, and stands in for Redis wherever Redis was only ever there to hold a counter. Each recipe below starts with the moment you'd reach for one, then shows the pattern, then says where it stops working. The examples with output under them execute against diskcache-oxide every time these docs are built, so they're known to work, not merely believed to.


# Caching

**[Caching expensive calls](caching-expensive-calls.md).** The cache-aside pattern, and [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize), which turns a function into a cached one keyed on its arguments.

**[Serving stale values while refreshing](stale-while-revalidate.md).** Keep a slow page or report available while one worker rebuilds it in the background, or refresh the hot keys ahead of time from a job, so readers never wait and the thundering herd is one worker.

**[Caching responses from other services](caching-http-responses.md).** Cache an upstream API's answers, honor its `max-age`, revalidate with ETags, and keep serving the last good answer when it's down.

**[Invalidating groups of keys](invalidating-groups.md).** Clear everything cached for one user, one product or one page at once: delete by key prefix, or bump a generation number so stale entries are never read again.

**[Storing your own types](custom-types.md).** UUIDs, enums, timedeltas, Paths, dataclasses and Pydantic models in the cache with one registration each, and no pickle.


# Coordinating workers

**[Running a job once across workers](run-once.md).** Eight gunicorn workers each schedule the same hourly job, and you want it to run once. [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) is atomic across processes, so exactly one of them claims it.

**[Rate limiting](rate-limiting.md).** Count requests per client in fixed time windows with [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) and [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr), shared by every worker on the machine, with no Redis.

**[Counting things](counters.md).** View counts, download totals and usage quotas with [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) and [decr()](../reference/Cache.decr.md#diskcache_oxide.Cache.decr), flushed to the database with [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) so no count is lost or doubled.

**[Single-use keys](single-use-keys.md).** Reset links, verification codes and idempotency keys: [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) hands a value to exactly one caller, and [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) accepts a key exactly once.


# State

**[Server-side sessions](sessions.md).** Sessions every worker can read, with a sliding idle timeout from [touch()](../reference/Cache.touch.md#diskcache_oxide.Cache.touch), an absolute lifetime, and log-out-everywhere by key prefix.


# Deployment and testing

**[Sharing a cache across web workers](web-workers.md).** Where to put the cache directory, when to open it with gunicorn, uvicorn, FastAPI, Flask, Django and Celery, how to keep a busy cache from slowing requests, and what to do at shutdown.

**[Using the cache from asyncio](asyncio.md).** Read inline, write off the loop, and keep transactions on one thread, in FastAPI, Starlette or aiohttp, until `AsyncCache` lands.

**[Testing code that uses a cache](testing.md).** pytest fixtures for a real cache in a temporary directory, a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) for tests that shouldn't cache at all, and a fake clock for testing expiry without sleeping.


# Coming later

More recipes will arrive with the features they need: tags and `AsyncCache` for asyncio apps. Locks, semaphores, throttles and a stampede-proof [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) are in [Coordination](../user-guide/coordination.md). [`FanoutCache`](../user-guide/fanoutcache.md) and [`transact()`](../user-guide/transactions.md) are here already and have their own guide pages. See the [roadmap](../user-guide/roadmap.md).
