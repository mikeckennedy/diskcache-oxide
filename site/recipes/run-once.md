# Running a job once across workers

At the top of the hour, eight gunicorn workers wake up at the same instant with the same idea: time for the hourly cleanup. Each one has its own copy of the scheduler, none of them knows the others exist, and the only thing they share is a cache directory. You want the job to run once, and it turns out the directory is enough.

[add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) stores a value only if the key is missing or expired, and it's atomic across every thread and process using the cache directory. When several callers race to add the same key, exactly one gets `True`. That makes the key a claim on the job.


# Claim the run

``` python
import os

from diskcache_oxide import Cache

cache = Cache('/var/cache/my-app')

def hourly_cleanup() -> None:
    if not cache.add('job:hourly-cleanup', os.getpid(), expire=55 * 60):
        return  # another worker has it this hour
    remove_stale_sessions()
```

The [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) is the important part. It decides when the job can be claimed again, and it's also what saves you if the worker that claimed the job crashes halfway through: the claim expires and the next run goes ahead. Set it a little shorter than the schedule's interval, so the next run is never blocked by the previous claim.

Storing `os.getpid()` (or a hostname and pid) as the value costs nothing and makes it easy to see who claimed the job: `cache.get('job:hourly-cleanup')`.


# See it work

Here eight threads race for the same claim. Processes behave the same way.


``` python
import tempfile
import threading

from diskcache_oxide import Cache

cache = Cache(tempfile.mkdtemp())
winners = []
start = threading.Barrier(8)

def worker(n: int) -> None:
    start.wait()
    if cache.add('job:hourly-cleanup', n, expire=3300):
        winners.append(n)

threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
for t in threads:
    t.start()
for t in threads:
    t.join()

print(f'{len(winners)} winner: worker {winners[0]}')
print('claim holds:', cache.get('job:hourly-cleanup'))
```


    1 winner: worker 7
    claim holds: 7


# Jobs that should run once, then again as soon as they're done

For work that isn't on a fixed schedule (process the queue, rebuild the search index), use [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) as a lease: a time limit after which a crashed worker's claim lapses. Delete the key when the work finishes, so the next request can claim it right away.

``` python
def rebuild_search_index() -> None:
    if not cache.add('job:search-index', os.getpid(), expire=10 * 60):
        return
    try:
        do_rebuild()
    finally:
        cache.delete('job:search-index')
```

Make the lease comfortably longer than the job ever takes. If the job outlives it, a second worker can start a second copy.


# Give claims their own small cache

Two things can make a claim vanish early, and both argue for keeping claims out of your main cache:

- **Switching caching off.** A [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) follows the rule "every entry expires the instant it's written", so its [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) always returns `True`. With caching off, every worker's claim succeeds and the job runs once per worker. That's harmless for idempotent jobs and wrong for jobs with side effects, like sending email.
- **Eviction.** When a cache is over its [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit), it evicts the least recently stored entries, and a claim is exactly the kind of small, rarely written entry that goes first. If it's evicted before it expires, a second worker can claim the job.

A separate cache directory just for coordination, which is never swapped for a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) and never evicts (`eviction_policy='none'`, so only expired claims go), avoids both:

``` python
coordination = Cache('/var/cache/my-app-coordination', eviction_policy='none')
```

A claim is the right tool when a worker that loses should skip the job. When it should wait its turn instead, use a `Lock` or a `BoundedSemaphore` from [Coordination](../user-guide/coordination.md), on the same coordination cache.
