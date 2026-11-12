# Counting things

A download counter is the simplest feature on the site, and the first one to start lying once there's more than one worker: each process counts what it saw, and the number on the page depends on which worker rendered it. Moving the count to the database fixes the lie and makes every download a write to your busiest table. A cache counter sits in between: [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) is atomic across every thread and process on the machine, costs tens of microseconds, and the totals can be moved to the database on a schedule, by one worker, without losing a single count on the way.


# Count from every worker


``` python
import tempfile
import threading

from diskcache_oxide import Cache

cache = Cache(tempfile.mkdtemp(), eviction_policy='none')   # counts are data: never evict them for size

def record_view(episode_id: int) -> None:
    cache.incr(f'views:episode:{episode_id}')

def viewer() -> None:
    for _ in range(250):
        record_view(512)

threads = [threading.Thread(target=viewer) for _ in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

print(cache.get('views:episode:512'), 'views, none lost')
```


    2000 views, none lost


[incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) starts a missing counter at `default` (0) and adds `delta` (1), so there's no setup step. Processes behave exactly like the threads here. The cache is opened with `eviction_policy='none'`, for the reason under [Things to know](#things-to-know).


# Flush the counts to the database

Totals belong in the database eventually: for reports, and so they survive a cleared cache. [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) reads a counter and removes it in one atomic step, so a view recorded while the flush runs lands in a fresh counter and is picked up next time. Nothing is counted twice or lost.


``` python
database: dict[int, int] = {}   # stands in for UPDATE episodes SET views = views + ? WHERE id = ?

def flush_views() -> None:
    for key in cache.iterkeys(prefix='views:episode:'):
        pending = cache.pop(key, 0, retry=True)   # a background job can wait out a busy moment
        if pending:
            episode_id = int(key.rsplit(':', 1)[1])
            database[episode_id] = database.get(episode_id, 0) + pending

record_view(513)
flush_views()
print('database:', database, '| pending in cache:', cache.get('views:episode:512', 0))
```


    database: {512: 2000, 513: 1} | pending in cache: 0


Run the flush from one place, a scheduled job that [one worker claims](run-once.md), every minute or so. To show a live total, add the pending count to the stored one: `database[id] + cache.get(key, 0)`.

The flush finds its counters by prefix, so their keys have to be strings. A tuple such as `('views', 512)` is a valid key, and [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) counts under it just as well, but `iterkeys(prefix=...)` matches `str` keys only and would never find it.


# Counters that clean themselves up

For per-day or per-month figures, put the period in the key and give each counter an expiry. Create it with [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) first: [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) keeps whatever expiry the key already has, and on a key it has to create, it sets none.


``` python
import datetime

def record_download(file_id: str, day: datetime.date) -> int:
    key = f'downloads:{file_id}:{day.isoformat()}'
    cache.add(key, 0, expire=40 * 24 * 3600)   # keep 40 days of history
    return cache.incr(key)

today = datetime.date(2026, 11, 8)
record_download('talk-python-491.mp3', today)
print(record_download('talk-python-491.mp3', today), 'downloads today')
```


    2 downloads today


To report a week, read the seven keys. Old days expire on their own, so the cache never fills with history.


# Quotas with [decr()](../reference/Cache.decr.md#diskcache_oxide.Cache.decr)

A quota is a counter that starts at the allowance and counts down:


``` python
def consume_quota(user_id: int, units: int = 1, allowance: int = 1000) -> bool:
    key = f'quota:{user_id}:2026-11'                       # the billing period, in a real app from the date
    cache.add(key, allowance, expire=40 * 24 * 3600)      # the first call this period sets the allowance
    return cache.decr(key, units) >= 0

print(consume_quota(42, units=999), consume_quota(42), consume_quota(42))
```


    True True False


Once the counter is below zero every later call is denied too, until the period's key is replaced. To let an admin grant more, [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) the key by the extra amount. `delta` can be a float, for fractional units.


# Several counters at once

Counting a request under several keys (total, per path, per status) is three [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) calls, and a reader can see the first land before the third. If that matters, put them in one [transaction](../user-guide/transactions.md): the block commits all of them together.


``` python
def record_request(path: str, status: int) -> None:
    with cache.transact():
        cache.incr('requests:total')
        cache.incr(f'requests:path:{path}')
        cache.incr(f'requests:status:{status}')

record_request('/episodes/512', 200)
print(cache.get('requests:total'), cache.get('requests:status:200'))
```


    1 1


On a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) a transaction covers one shard, chosen by `transact(key=...)`, and an [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) inside it on a key in another shard raises `ValueError`. Keep counters that must change together in a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache).


# Things to know

- **Don't let counts be evicted.** A cache at its [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) evicts the least recently stored entries first. Each [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) counts as a store, so a busy counter is safe, but a quiet one is the oldest thing in the cache: an episode's views waiting for the next flush, a quota halfway through its period, last month's downloads. An evicted quota comes back as a fresh allowance. Open the counters' cache with `eviction_policy='none'`, as the examples do, and let expiry do the cleaning.
- **A missing counter reads as `default`.** When a missing counter is a bug rather than a zero, pass `default=None` and [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) raises `KeyError` instead.
- **Counters need a real cache.** A [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) has nothing to count in: its [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) returns `default + delta` every time, so every view would be the first. Keep counters in a cache that's never switched off, which [`Caches`](../user-guide/caches.md) lets you do per name.
- **`durable=True` drops counts silently.** A failed [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) returns `default + delta` so a rate limiter fails open, but for a quota or a view count that's a lost increment with only a log line to show for it. Let these raise, and catch [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) where you call them, or pass `retry=True` where waiting is fine, as the flush does.
- **Not an id generator.** Two callers can get the same number back under `durable=True`, and a cleared cache restarts at zero. Ids come from the database.
- **One machine.** Every worker on a host shares the counts; two hosts behind a load balancer each have their own, so flush each host's counts to the database and sum there.
