# Work queues

A web request needs a thumbnail rendered, an email sent or a transcript cleaned up, and none of it should make the user wait. The usual answer is a queue and a worker, and the usual queue is Redis. On one machine a cache directory does the job: [push()](../reference/Cache.push.md#diskcache_oxide.Cache.push) adds an item at one end, [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) takes one from the other, and every process that opens the directory shares the queue.


``` python
import tempfile

from diskcache_oxide import Cache

jobs = Cache(tempfile.mkdtemp(), eviction_policy='none')

for episode in (510, 511, 512):
    jobs.push({'episode': episode, 'step': 'transcode'}, prefix='transcode')

key, job = jobs.pull(prefix='transcode')
print(key, job)
```


    transcode-500000000000000 {'episode': 510, 'step': 'transcode'}


# How it works

- **A queue is a range of keys.** `push(value, prefix='transcode')` stores the item under `'transcode-500000000000000'`, then `...001`, and so on; with no prefix the keys are ints. `side='front'` pushes to the other end, and `pull(side='back')` takes the newest item, so the same calls make a stack or a deque.
- **Each item goes to exactly one worker.** [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) reads the front item, then deletes exactly the version it read; a worker that loses that race to another reads the new front, and since every lost race is another worker's progress, the queue always drains. Run as many workers, in as many processes, as you like.
- **Nothing is lost to a reader that can't read it.** An item this process can't decode stays at the front for a worker that can. A pickled item on a `pickle=False` cache raises [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError). A value of a type only a newer deploy has registered raises too, or with `on_decode_error='miss'`, [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) returns the default. Either way this worker can take nothing behind that item until a worker that can read it pulls it. An item no build could read (damaged bytes) is removed: by default [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) then raises [CorruptValueError](../reference/CorruptValueError.md#diskcache_oxide.CorruptValueError), and with `on_decode_error='miss'` it logs the item and moves on to the next one.
- **[peek()](../reference/Cache.peek.md#diskcache_oxide.Cache.peek) looks without taking.** It takes no lock in the normal case, so any number of processes can watch a queue at once. [peekitem()](../reference/Cache.peekitem.md#diskcache_oxide.Cache.peekitem) gives the newest or oldest entry of the whole cache, queue or not.


``` python
print(jobs.peek(prefix='transcode'))                # the next item, left in place
print(jobs.peek(prefix='transcode', side='back'))   # the newest
print(len(jobs))                                    # every entry in the cache
```


    ('transcode-500000000000001', {'episode': 511, 'step': 'transcode'})
    ('transcode-500000000000002', {'episode': 512, 'step': 'transcode'})
    2


# A worker loop

[pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) returns `(None, None)` when the queue is empty, or the `default` you pass.


``` python
import time

def work(job: dict) -> None:
    print('transcoding', job['episode'])

def drain(cache: Cache, idle_sleep: float = 0.5, stop_after_idle: int = 1) -> None:
    idle = 0
    while idle < stop_after_idle:
        key, job = cache.pull(prefix='transcode')
        if key is None:
            idle += 1
            time.sleep(idle_sleep)
            continue
        idle = 0
        work(job)

drain(jobs, idle_sleep=0.01)
```


    transcoding 511
    transcoding 512


A real worker runs with no idle limit and sleeps between empty pulls; there's no blocking pull to wait on.


# Jobs that must survive a crash

A pulled item is gone as soon as [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) returns, so a worker that crashes halfway through `work()` loses it. If a job must survive that, give each job an id of its own and move it to a running record in the same transaction as the pull, so no crash can fall between the two. Delete the record when the work is done, and have a sweeper push back any record older than its lease:


``` python
import uuid

def enqueue(cache: Cache, episode: int) -> None:
    cache.push({'id': uuid.uuid4().hex, 'episode': episode, 'step': 'transcode'}, prefix='transcode')

def take(cache: Cache) -> dict | None:
    """The next job, recorded as running in the same transaction; None if the queue is empty."""
    with cache.transact():
        key, job = cache.pull(prefix='transcode')
        if key is None:
            return None
        cache.set(f"running:{job['id']}", (time.time(), job))   # no expire: the sweeper decides
    return job

def finish(cache: Cache, job: dict) -> None:
    cache.delete(f"running:{job['id']}")

def requeue_stale(cache: Cache, lease: float = 600) -> int:
    requeued = 0
    for record in cache.iterkeys(prefix='running:'):   # deleting while iterating is safe
        with cache.transact():
            started, job = cache.get(record, (None, None))   # read again: the worker may have finished
            if started is not None and time.time() - started > lease:
                cache.push(job, prefix='transcode', side='front')
                cache.delete(record)
                requeued += 1
    return requeued

enqueue(jobs, 513)
if (job := take(jobs)) is not None:
    work(job)
    finish(jobs, job)

enqueue(jobs, 514)
job = take(jobs)                          # this worker dies before it calls finish()
print(requeue_stale(jobs, lease=0), 'requeued:', jobs.peek(prefix='transcode'))

if (job := take(jobs)) is not None:       # another worker picks it up
    work(job)
    finish(jobs, job)
```


    transcoding 513
    1 requeued: ('transcode-500000000000000', {'id': 'a43a9d91c77b4252a73ec3d81b4c446b', 'episode': 514, 'step': 'transcode'})
    transcoding 514


The sweeper reads each record again inside its transaction, so a job that finished while the sweeper was looking isn't pushed back. One that is still running past its lease is, and then runs twice: make the lease longer than your slowest job, and make the work safe to repeat. Run the sweeper from one place, a scheduled job that [one worker claims](run-once.md). The item's key isn't a lasting id (see below), which is why the job carries its own.


# Several queues, and priorities

Each prefix is its own queue, in the same directory. A worker that serves more than one, or serves urgent work first, tries them in order:


``` python
jobs.push({'to': 'sam@example.com', 'template': 'welcome'}, prefix='email')
jobs.push({'episode': 515, 'step': 'transcode'}, prefix='transcode')
jobs.push({'episode': 516, 'step': 'transcode'}, prefix='urgent')

def next_job(cache: Cache, queues: tuple[str, ...] = ('urgent', 'transcode', 'email')) -> tuple[str, dict] | None:
    for queue in queues:
        key, job = cache.pull(prefix=queue)
        if key is not None:
            return queue, job
    return None

while (taken := next_job(jobs)) is not None:
    print(taken)
```


    ('urgent', {'episode': 516, 'step': 'transcode'})
    ('transcode', {'episode': 515, 'step': 'transcode'})
    ('email', {'to': 'sam@example.com', 'template': 'welcome'})


While the urgent queue keeps filling, the others wait. To count what's waiting, iterate the queue's keys: `iterkeys(prefix='transcode-')` is an indexed range scan over that queue alone. Items that expired but haven't been removed yet are counted too, as are any other keys that start with `'transcode-'`.


# Cancelling and expiring jobs

A job pushed with `tag=` can be cancelled with the rest of its group, wherever they are in the queue, by `evict(tag)`. A job that's pointless after a deadline, a "your export is ready" email, say, can be pushed with `expire=`, and [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) passes over it once it has expired.


``` python
for episode in (520, 521):
    jobs.push({'episode': episode, 'step': 'transcode'}, prefix='transcode', tag='upload:batch-7')
jobs.push({'episode': 522, 'step': 'transcode'}, prefix='transcode')

print(jobs.evict('upload:batch-7'), 'cancelled; next:', jobs.pull(prefix='transcode'))
```


    2 cancelled; next: ('transcode-500000000000002', {'episode': 522, 'step': 'transcode'})


`pull(tag=True)` and `pull(expire_time=True)` return the item's tag and expiry beside it, as [get()](../reference/Cache.get.md#diskcache_oxide.Cache.get) does: `((key, value), expire_time, tag)`.


# In an app that uses [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache)

The queue calls are on [Cache](../reference/Cache.md#diskcache_oxide.Cache) only: a queue spread over shards would have no order. If your app's main cache is a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache), open the queue as a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) in a directory of its own. A [Cache](../reference/Cache.md#diskcache_oxide.Cache) opened on the [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache)'s directory raises [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError).

``` python
cache = FanoutCache('/var/cache/my-app', shards=8)
jobs = Cache('/var/cache/my-app-jobs', eviction_policy='none')
```

A separate directory is a good idea in any app: the queue gets its own eviction policy, and switching the main cache off can't touch it.


# Things to know

- **Open the queue's cache with `eviction_policy='none'`.** Least-recently-stored eviction removes the oldest entries first, and the oldest entry in a queue is the next one to be pulled: a full cache would silently drop the front of the queue. Under `'none'` nothing is evicted for size; only items you gave an [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) ever vanish.
- **Use a prefix no other key starts with.** Only keys of exactly the queue's shape (`'{prefix}-'` and 15 digits) belong to it, so an ordinary key like `'transcode-settings'` or `'transcode-512'`, or an item of another queue named `'transcode-hd'`, is never pulled or numbered as one of its items (D-054). Keys whose next character after the dash is a digit, like `'transcode-512'`, do sort inside the queue's range, though, and every queue call has to step past them.
- **Expired items are passed over.** An item pushed with `expire=` is never pulled or peeked after it expires. It stays on disk, and keeps its number, until [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) or the cleanup that later writes do removes it.
- **Keys are reused.** After the front item is pulled, a push to the front takes its number again, so don't use an item's key as a lasting id.
- **Inside a transaction,** [push()](../reference/Cache.push.md#diskcache_oxide.Cache.push) and [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) join it, and a rollback puts a pulled item back, after your code has already seen it.
- **Keep the queue out of [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache)'s reach.** A [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) has the queue calls, but its queue is always empty and its pushes store nothing, so a setting that switches caching off would drop every job. Don't register the queue's cache where it can be switched off.
- **`durable=True` can drop a push.** On a durable handle a [push()](../reference/Cache.push.md#diskcache_oxide.Cache.push) that fails returns `None`, and the job was never stored. Check the return value, or open the queue's cache without [durable](../reference/Cache.durable.md#diskcache_oxide.Cache.durable).
- **Past the end,** after 500 trillion pushes to one side, [push()](../reference/Cache.push.md#diskcache_oxide.Cache.push) raises `OverflowError` rather than overwrite anything (D-057).
