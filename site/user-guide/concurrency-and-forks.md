# Concurrency and forks

A production Python web app is a crowd. Several worker processes, often several threads in each, a pre-forking server that creates and recycles those workers while they're busy, and a cron job or an admin script opening the same cache on the side. More and more, it's also free-threaded Python, where the threads really do run at the same time. diskcache-oxide is designed for all of that at once, and its test suite runs each of these cases on every change, because this is where cache bugs hide: not in [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) and [set](../reference/Cache.set.md#diskcache_oxide.Cache.set), but in what happens when two of them collide.


# Threads

One [Cache](../reference/Cache.md#diskcache_oxide.Cache) can be shared by any number of threads. There's nothing to lock and no per-thread setup.

- **Free-threaded ready.** The extension declares `gil_used = false` and never relies on the GIL for correctness. Its shared state is guarded by Rust's own locks and atomics. Every change is tested on 3.14, 3.14t, 3.15 and 3.15t.
- **Waits don't hold up Python.** Anything that can wait, such as a busy write lock, reading a large value file, or a chunk of [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear), releases the interpreter while it waits, so other Python threads keep running on GIL builds too.
- **Threads queue fairly.** Writers in one process queue on an in-process lock for each database file instead of all retrying against SQLite's busy handler, and long maintenance calls like [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) and [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) step aside between chunks so a waiting write gets in.


## A bounded connection pool

diskcache opens a SQLite connection for every thread that touches the cache and keeps it. With 64 threads that's 131 open files in the cache directory, and a server with enough threads runs out of file descriptors.

diskcache-oxide shares a pool of connections per database file across every handle in the process:

``` python
cache = Cache('/var/cache/my-app', max_connections=8)   # the default
```

The pool never holds more than `max_connections` connections, so at most `2 × max_connections + 1` files are open in the cache directory, however many threads you run. A thread that finds every connection busy waits for one, up to the cache's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout). When the last handle on a directory closes, every connection closes with it, whichever threads used them.


# Processes

Any number of processes can open the same directory, each with its own [Cache](../reference/Cache.md#diskcache_oxide.Cache). That's the normal deployment: every gunicorn or uvicorn worker opens the cache at startup. A [Cache](../reference/Cache.md#diskcache_oxide.Cache) or [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) also pickles, so you can pass one to a `multiprocessing` or `ProcessPoolExecutor` worker: the copy reopens the same directory with the options the handle was opened with.

- **WAL mode, always.** SQLite runs in write-ahead-log mode, so readers never block writers and writers never block readers. One writer at a time commits to each database file. diskcache-oxide checks that WAL mode is really on when it opens a cache, and fails loudly if SQLite declined it (some FUSE mounts do). That check can't detect a network filesystem: on NFS, SMB, EFS or Azure Files SQLite usually accepts WAL and its locks then don't hold, so keep caches on a local disk.
- **Atomic across processes.** [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add), [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr), [decr()](../reference/Cache.decr.md#diskcache_oxide.Cache.decr) and every other write is a single transaction. Two processes can't both win an [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add), and two [incr()](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) calls never lose an update. [pop()](../reference/Cache.pop.md#diskcache_oxide.Cache.pop) is a read and then a transaction that deletes exactly the version it read, so two processes can't both get the same value.
- **Maintenance that doesn't destroy work.** Every write gets a new sequence number. [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) and [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) note the latest number when they start and only touch older rows, so writes from other processes that land while they run are kept.
- **Large values are always whole.** A process reading a large value while another overwrites it gets the old value or the new one, never a miss. (diskcache's standalone reproduction of that race sees about 40 misses in every 325,000 reads.)
- **One write lock per database file.** Every writer to a directory queues on its lock, so with many processes writing often a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) stops scaling at a couple of processes. A [`FanoutCache`](fanoutcache.md) has one lock per shard and keeps scaling (5.7x a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) at 8 processes on a 99:1 read/write mix). It doesn't change the thread picture below: threads sharing one handle on free-threaded Python are not lifted by it today.
- **Opening never writes.** Opening a cache with unchanged settings and reading from it never takes the write lock or touches the database file, so a read-mostly worker costs the writers nothing.


# Forks

This section is about macOS and Linux. Windows has no `fork()`: there `multiprocessing` starts every worker fresh, each opens its own handle, and nothing here applies.

Pre-forking servers (gunicorn, Celery's prefork pool, `multiprocessing` with the fork start method) create workers with `fork()`, which copies the whole process. Picture a thread halfway through a write at the instant the server forks. The child is a photocopy of the parent, SQLite's note that this process holds the write lock included. But only the forking thread exists in the child, so the thread that was going to release that lock isn't there. In diskcache, that child's first write waits for a ghost: it stalls for the full timeout (60 seconds by default). Under a busy read/write load, that hits 20-50% of forks.

diskcache-oxide prevents it with a **fork barrier**, a crossing guard installed when `diskcache_oxide` is imported. It stops traffic in a specific order:

1.  **Before the fork,** the barrier waits for any write transactions already running in other threads to commit, and holds off new ones. It waits with the interpreter released, so the threads it waits for can finish. Then it closes the idle pooled connections, so the child doesn't inherit SQLite's lock bookkeeping for those files.
2.  **In the parent,** it lets the held-off writes continue.
3.  **In the child,** it drops any connection still inherited without closing it (closing a connection inherited across a fork is unsafe in SQLite), and opens fresh ones on first use.

The result: whatever other threads were doing at the moment of the fork, the child is born with no locks and no debts, and can read and write right away. The barrier covers every [Cache](../reference/Cache.md#diskcache_oxide.Cache) in the process, not just the one you're holding. Your own `os.register_at_fork(after_in_child=...)` hooks can use a cache too, whether they were registered before or after `diskcache_oxide` was imported.

Closing the idle connections matters for more than file descriptors. SQLite keeps one lock record per database file per process, and a new connection that finds a record saying the process already holds a shared lock takes no lock of its own. A child that inherited such a record would run with no real locks, and once the parent closed its cache, another process's last close could delete the write-ahead log under the child and lose its writes. The cost is that the parent reopens connections after each fork.

The only connections a child can still inherit are the ones the forking thread itself was using: a transaction it holds (see below), or an operation it forked from inside, such as from a callback. The child leaves those file descriptors open by design.


## How long a call waits for a fork

A fork counts against [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout). A cache call behind a pending fork waits for it through the call's own [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) and then raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), so [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) has to exceed the work in flight, plus the fork, plus the interpreter hand-off. A fork takes about a millisecond in a quiet small process, but tens to hundreds of milliseconds beside busy threads on a GIL build (the forking thread has to get the interpreter back), longer in a large process, and it waits for work in flight such as a forced checkpoint. A [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) under that sees [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) during ordinary forks, and a loop on `timeout=0` adds to the problem by being one of those busy threads.

A fork waits for every [transaction](transactions.md) another thread holds, with no timeout of its own, so a long transaction delays every fork in the process. Code that holds a transaction or runs inside a pickle hook (or a registered type's `decode` function, which runs in the same place), and waits for another thread's cache call, can keep a fork from finishing: the fork waits for that code, and the other thread waits for the fork. That other thread's call now raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), and the fork finishes once your code lets go. Until it does, `os.fork()` doesn't return, a second `os.fork()` queues behind it, and every thread with no transaction or hook of its own gets [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) on every [Cache](../reference/Cache.md#diskcache_oxide.Cache) in the process, after using up its whole [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) (60 seconds by default). Other processes aren't affected. If the code never reacts (a hook that retries forever, or one blocked on something that isn't a cache call), the fork stays stuck. A hook stuck in C code that holds the GIL stops everything, as it would without a cache.

Two calls don't raise. [checkpoint()](../reference/Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint) returns `False` when a fork keeps it from running. [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) takes at most about 2 seconds behind a stuck fork, 1 for the checkpoint and 1 for the release, and `del` about 1, whatever [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) is. An ordinary fork ends well inside that, so [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) waits it out as usual. If the fork outlasts it, [close()](../reference/Cache.close.md#diskcache_oxide.Cache.close) returns anyway and the connections close when the fork ends.

Before-fork hooks run in reverse registration order, and `logging` and `concurrent.futures` each take a lock in theirs and keep it until the fork is over. `diskcache_oxide` makes sure its own hook runs before those two, so they stay usable while a fork waits for your code: a pickle hook or a `decode` function can still log or submit work. Two things it can't fix. Another library's hook that takes a lock and is registered after `diskcache_oxide` stays held while a fork is stuck. And a thread that calls the cache while holding `logging`'s module lock can hold up a fork until that call's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout).

With [`durable=True`](getting-started.md#a-cache-that-cant-take-a-request-down), a [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) behind a fork is a miss or `False` rather than an exception on a single-key call, so a [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) shorter than this makes ordinary forks into misses. A pickle hook or a registered type's `decode` that re-raises the [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) it got from another thread's call lets go of its level under any policy, which clears the stuck fork. What `durable=True` adds is that the request doesn't fail: that [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) is a miss, with the row kept.


## Forking from inside your own transaction

CPython won't let a library refuse a fork, so the barrier can't wait for a [transaction](transactions.md) held by the very thread that is forking. The child's copy of that transaction is poisoned: anything done inside it raises `RuntimeError` and nothing commits, and the child's writes to that database raise `RuntimeError` straight away instead of stalling for a minute. A [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) in the child doesn't join the inherited transaction either: it raises `RuntimeError` too. The parent's block carries on and commits as usual, and the parent gets a `RuntimeWarning`. A clear error at the call site beats a mysterious 60-second hang in a fresh worker.


# Free-threaded performance today

On free-threaded 3.14t, diskcache-oxide is already faster than diskcache at every thread count, and at a 1,000:1 read/write mix 8 threads reach about 1.4 million operations per second. But that's only about 1.6× its single-thread number, so threads don't scale the way they should yet. Profiling shows where the time goes: about 60% of a cached read is one system call SQLite makes to take its shared-memory read lock, under a process-wide mutex. Removing that wall is on the [roadmap](roadmap.md), after the Django backend. A [`FanoutCache`](fanoutcache.md) doesn't remove it for threads sharing one handle either; that is being worked on. See [Performance](performance.md).
