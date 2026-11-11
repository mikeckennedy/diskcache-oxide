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
- **Nothing is lost to a reader that can't read it.** An item this process can't decode, a pickled value on a `pickle=False` cache or a value of a type a newer deploy registered, stays at the front for a worker that can, and [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) raises (or, with `on_decode_error='miss'`, returns the default). An item no build could read (damaged bytes) is removed: by default [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) then raises [CorruptValueError](../reference/CorruptValueError.md#diskcache_oxide.CorruptValueError), and with `on_decode_error='miss'` it logs the item and moves on to the next one.
- **[peek()](../reference/Cache.peek.md#diskcache_oxide.Cache.peek) looks without taking.** It takes no lock in the normal case, so any number of processes can watch a queue at once. [peekitem()](../reference/Cache.peekitem.md#diskcache_oxide.Cache.peekitem) gives the newest or oldest entry of the whole cache.


``` python
print(jobs.peek(prefix='transcode'))                # the next item, left in place
print(jobs.peek(prefix='transcode', side='back'))   # the newest
print(len(jobs))
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


A pulled item is gone as soon as [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) returns, so a worker that crashes halfway through `work()` loses it. If a job must survive that, give each job an id of its own and move it to a running record in the same transaction as the pull, so no crash can fall between the two. Delete the record when the work is done, and have a sweeper push back any record older than its lease:


``` python
import uuid

jobs.push({'id': uuid.uuid4().hex, 'episode': 513, 'step': 'transcode'}, prefix='transcode')

with jobs.transact():
    key, job = jobs.pull(prefix='transcode')
    if key is not None:
        jobs.set(f"running:{job['id']}", (time.time(), job))   # no expire: the sweeper decides

work(job)
jobs.delete(f"running:{job['id']}")

def requeue_stale(cache: Cache, lease: float = 600) -> None:
    for record in cache.iterkeys(prefix='running:'):
        started, stale = cache.get(record, (None, None))
        if started is not None and time.time() - started > lease:
            with cache.transact():
                cache.push(stale, prefix='transcode', side='front')
                cache.delete(record)
```


    transcoding 513


The key isn't a lasting id (see below), which is why the job carries its own.


# Things to know

- **Open the queue's cache with `eviction_policy='none'`.** Least-recently-stored eviction removes the oldest entries first, and the oldest entry in a queue is the next one to be pulled: a full cache would silently drop the front of the queue. Under `'none'` nothing is evicted for size; only items you gave an [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) ever vanish.
- **Use a prefix no other key starts with.** Only keys of exactly the queue's shape (`'{prefix}-'` and 15 digits) belong to it, so an ordinary key like `'transcode-settings'`, or another queue's `'transcode-hd-...'`, is never pulled or numbered as one of its items (D-054). But such keys still sit inside the queue's range, and each call steps past them.
- **Expired items are passed over.** An item pushed with `expire=` is never pulled or peeked after it expires, and [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) or a later write removes it.
- **Keys are reused.** After the front item is pulled, a push to the front takes its number again, so don't use an item's key as a lasting id.
- **Inside a transaction,** [push()](../reference/Cache.push.md#diskcache_oxide.Cache.push) and [pull()](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) join it, and a rollback puts a pulled item back, after your code has already seen it.
- **Not on a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache).** A queue spread over shards would have no order, so the queue calls are on [Cache](../reference/Cache.md#diskcache_oxide.Cache) only (and on [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache), where a queue is always empty and pushes store nothing).
- **Past the end,** after 500 trillion pushes to one side, [push()](../reference/Cache.push.md#diskcache_oxide.Cache.push) raises `OverflowError` rather than overwrite anything (D-057).
