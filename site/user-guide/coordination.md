# Coordination: locks, semaphores and throttles

A cache directory is shared by every process on the machine that opens it, and [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add), [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) and [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) are atomic across all of them. That makes it a place to coordinate work as well as cache it: one worker at a time on a job, at most a few at once on a slow API, a rate limit every worker respects. diskcache-oxide ships diskcache's coordination recipes for that, under the same names.

``` python
from diskcache_oxide import Cache, Lock, RLock, BoundedSemaphore, Averager, throttle, barrier

locks = Cache('/var/cache/my-app/locks', eviction_policy='none')
```

Two common jobs don't need a lock at all. When only one worker should run a job and the others should skip it, an [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) claim is simpler: see [Running a job once across workers](../recipes/run-once.md). To hand work items to whichever worker is free, use a queue: see [Work queues](../recipes/work-queues.md).


# Lock

``` python
lock = Lock(locks, 'report:nightly', expire=600)

with lock:
    build_the_report()
```

One holder at a time, in any thread of any process. `acquire()` waits, polling every millisecond, until it gets the lock; `release()` gives it back; `locked()` says whether anyone holds it. It isn't reentrant: the holder taking it again waits for itself. Give it an [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire), so a holder that crashes without releasing doesn't keep the lock forever.

`RLock` is the reentrant one: the thread holding it may take it again, and must release it as many times. Its owner is the process and thread, with a per-process nonce, so another thread, another process, a forked child, or a container on the same volume that happens to have the same pid can never release or re-enter it. Releasing a lock you don't hold raises `AssertionError`.


# BoundedSemaphore

``` python
api_slots = BoundedSemaphore(locks, 'api:partner', value=4)

with api_slots:
    call_the_partner_api()
```

At most `value` holders at once, everywhere. Releasing more times than it was acquired raises `AssertionError`.


# throttle

``` python
@throttle(locks, count=10, seconds=1)
def send_webhook(event): ...
```

At most `count` calls every `seconds`, across every process: a token bucket stored under the function's qualified name (or `name=`). A call with no token waits until one is due. Two fixes over diskcache's: a bucket that expired or was cleared counts as full rather than raising `TypeError`, and decorating the function adds the bucket only if it isn't there, so every worker that imports the module doesn't refill it.


# barrier

``` python
@barrier(locks, Lock)
def migrate(): ...
```

Runs the function under a lock made by the factory you pass (`Lock`, `RLock`, `BoundedSemaphore`, or your own), keyed by the function's qualified name: one caller at a time.


# Averager

``` python
latency = Averager(locks, 'latency:checkout')
latency.add(0.080)
latency.add(0.120)
latency.get()  # 0.1
```

A running average whose total and count are updated together in one transaction, so every process adding to it agrees.


# memoize_stampede

`memoize_stampede(cache, expire)` is [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) with stampede protection: a result close to expiring is recomputed early, in a background thread, by one caller, while everyone else keeps getting the cached value. It's a cache, not a lock, so it works on a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) and a `durable=True` cache as [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) does. See [Caching expensive calls](../recipes/caching-expensive-calls.md).


# Things to know

- **Use a cache of its own for coordination, with `eviction_policy='none'`.** Lock and semaphore keys are cache entries: least-recently-stored culling removes the oldest first, and a lock held a long time is old, so a full cache can evict a held lock. Under `'none'` nothing is ever evicted for size, only expired entries are removed, so give every lock an [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire). [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) releases every lock in the cache, so keep other data out of it: `Cache('/var/cache/my-app/locks', eviction_policy='none')`.
- **Not on a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) or a `durable=True` cache.** The recipes refuse them with `TypeError`: with caching off every caller would hold the lock at once, and a durable cache could drop the [delete](../reference/Cache.delete.md#diskcache_oxide.Cache.delete) that releases one, leaving it held forever.
- **Not while waiting inside your own transaction.** A recipe that would have to wait inside a [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) block on its own cache raises `RuntimeError`: waiting there holds the cache's write lock, so no other process could ever release the lock you're waiting for. Inside a transaction on a different cache, a contended recipe raises [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy) at once rather than waiting ([D-030](divergences.md)).
- **On a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache)** each recipe locks only its key's shard.
- `tag=` tags every entry a recipe writes, so `evict(tag)` releases them all at once. `barrier` passes it to the lock factory only when it isn't `None`.
