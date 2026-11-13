# Rate limiting

Someone has pointed a script at your search endpoint. It's a polite script: it stays under your limit of 100 requests a minute. Per worker. With eight workers and an in-memory counter in each, the limit you meant is 100 and the limit you have is 800. To limit each client for real, every worker process has to see the same count, and Redis is the usual fix. A diskcache-oxide counter is shared by every worker on the machine, and [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) is atomic across processes, so concurrent requests never lose a count.


# A fixed-window limiter

Each client gets a counter for the current window, with the window's number in the key. When the window ends, requests move on to a new key and the old one simply expires.


``` python
import tempfile

from diskcache_oxide import Cache

now = [1_000_000.0]  # a fake clock, so this example can move time forward
cache = Cache(tempfile.mkdtemp(), clock=lambda: now[0])

def allow_request(client: str, limit: int = 5, window: int = 60) -> bool:
    current = int(now[0] // window)          # in a real app: int(time.time() // window)
    key = f'rate:{client}:{current}'
    cache.add(key, 0, expire=window * 2)     # start the window's counter, once
    return cache.incr(key) <= limit

print([allow_request('203.0.113.9') for _ in range(7)])

now[0] += 60  # the next window
print(allow_request('203.0.113.9'))
```


    [True, True, True, True, True, False, False]
    True


Why it's built this way:

- **[add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) then [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr).** [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) creates the counter with an expiry the first time and does nothing after that. [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) keeps a key's existing expiry, so the counter expires on schedule however many times it's incremented. Calling [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) alone would create a counter that never expires.
- **The window number is in the key.** A new window means a new key, so no reset is needed, and there's no race where one request resets the counter while another increments it.
- **The expiry is two windows long,** so a counter can't expire while its window is still current. Expired counters are cleaned up by [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) or eviction like any other entry.

In a real app, use `time.time()` and leave out `clock=`; the fake clock is only here so the example can jump to the next minute.


# Using it in a view

``` python
from fastapi import FastAPI, HTTPException, Request

app = FastAPI()

@app.get('/api/search')
def search(request: Request, q: str) -> dict:
    if not allow_request(request.client.host, limit=100, window=60):
        raise HTTPException(status_code=429, detail='Too many requests')
    return run_search(q)
```


# Things to know

- **Fixed windows allow short bursts at the boundary.** A client can make 100 requests at the end of one minute and 100 more at the start of the next. For most APIs that's fine. If it isn't, check both the current and the previous window and weight the previous one by how much of it overlaps the last 60 seconds (a sliding-window estimate).
- **It's per machine.** Every worker on one machine shares the count, but two machines behind a load balancer each have their own. Sharing one cache across machines would take the network server on the [roadmap](../user-guide/roadmap.md), which is still speculative.
- **Fail open on a busy cache.** If you set a short [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) on the cache so that requests never wait long, catch [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) and allow the request rather than failing it:

``` python
from diskcache_oxide import Timeout

try:
    allowed = allow_request(client)
except Timeout:
    allowed = True
```

- **Don't rate limit with a cache that might be a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache).** Its [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) always returns `default + delta`, so every request would be allowed. That's often what you want with caching switched off, but make it a deliberate choice.

When the caller should wait for its turn instead of getting a 429, as for outgoing webhooks or calls to a partner API, use [throttle](../reference/throttle.md#diskcache_oxide.throttle) from [Coordination](../user-guide/coordination.md#throttle): a decorator that holds a function to `count` calls every `seconds`, across every process.
