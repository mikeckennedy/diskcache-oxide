<p align="center">
  <img src="https://mkennedy-shared.nyc3.digitaloceanspaces.com/github/diskcache-oxide-slogan.jpg" alt="diskcache-oxide: Python cache. Rust engine. SQLite storage." width="850">
</p>

# diskcache-oxide

**A shared, durable cache for Python apps, rebuilt in Rust for many cores, many processes and free-threaded Python.**

Every Python web app arrives at the same situation. A query that was fine at launch is now the slowest thing on the page, so you cache its result in a dict. Then gunicorn starts four workers and there are four dicts, each warming up alone and forgetting everything on restart. So you reach for Redis, and now there's a server to deploy, secure and monitor, to remember the answer to one query. [python-diskcache](https://github.com/grantjenks/python-diskcache) showed the third way: a cache that is just a directory, shared by every thread and process on the machine, still there after a restart. But diskcache has had no commits since 2023, and its issues and pull requests go unanswered. diskcache-oxide keeps diskcache's API and rebuilds everything underneath it: a Rust engine, SQLite storage, and the thinnest possible Python layer.

**Docs: <https://mkennedy.codes/docs/diskcache-oxide/>**

```python
import os

from diskcache_oxide import Cache

cache = Cache('/tmp/my-app-cache', durable=True)  # every worker opens the same directory

popular = cache.get('episodes:popular')
if popular is None:
    popular = ['ep-501', 'ep-500']  # stand-in for a slow query
    cache.set('episodes:popular', popular, expire=300)

# Only one worker runs the hourly job: add() is atomic across processes.
if cache.add('job:refresh-feeds', os.getpid(), expire=3600):
    print('this worker refreshes the feeds')

cache.close()  # at shutdown


@cache.memoize(expire=600)
def episode(number: int) -> dict:
    return {'number': number}  # stand-in for a database read
```

## An app with several caches

Most apps end up with more than one cache: rendered pages, API responses, sessions, rate limits, each with its own size and settings. `Caches` is the registry for them. You register each cache once at startup, and a lookup by name opens it on first use and returns the same object every time after that. If a cache can't open (a full disk, a permissions problem, a cache left behind by an older release), the lookup returns a `NullCache` instead of raising, so requests carry on without that cache while the error is logged, and the registry tries again every 10 seconds until the real one is back. One call switches a cache off for a load test, and `stats()` gives an admin page every cache's entries, size and errors.

```python
from diskcache_oxide import Caches

caches = Caches('/var/cache/my-app', timeout=1.0)  # settings here apply to every cache
caches.register('pages', size_limit=2 * 2**30)
caches.register('sessions', eviction_policy='none')  # never evicted for size
caches.register('rate_limits', shards=8)  # a FanoutCache, for many writers

html = caches['pages'].get('/about')  # opens on first use, the same object after that
caches['sessions'].set('session:9f2c', {'user': 42}, expire=3600)
caches.fanout('rate_limits').incr('ip:203.0.113.7')

caches.disable('pages')  # a load test without the page cache: no call site changes

for name, stat in caches.stats().items():  # for an admin page
    print(name, stat.count, stat.volume, stat.error)

caches.close_all()  # at shutdown
```

Talk Python runs its eight caches this way. [Named caches](https://mkennedy.codes/docs/diskcache-oxide/user-guide/caches.html) covers the registry in full.

## Status

diskcache-oxide runs in production at [Talk Python](https://talkpython.fm), [Python Bytes](https://pythonbytes.fm) and [Talk Python Training](https://training.talkpython.fm). Before each release it passes the same conformance suite run against diskcache 5.6.3 itself (apart from the [documented divergences](https://mkennedy.codes/docs/diskcache-oxide/user-guide/divergences.html)), a sanitizer pass over the whole codebase (Miri, ThreadSanitizer including a free-threaded CPython, AddressSanitizer and LeakSanitizer), and a 24-hour soak that throws processes, threads, forks and `SIGKILL` at one directory.

Most of diskcache's API is here: `Cache` and `FanoutCache`, keys of every native type, `memoize`, tags and `evict()`, `transact()`, the queue calls `push`, `pull` and `peek`, `retry=`, `stats()`, `check()`, `eviction_policy='none'`, and the coordination recipes (`Lock`, `RLock`, `BoundedSemaphore`, `throttle`, `barrier`, `Averager` and `memoize_stampede`). Still to come: `Deque` and `Index`, reading a large value as a file (`read=`), `AsyncCache`, and a Django cache backend. Custom `disk=` classes and least-recently-used eviction aren't coming; [where it's going](#where-its-going) says why.

## Install

```bash
pip install diskcache-oxide
```

- **Python:** CPython 3.14 or 3.15, including the free-threaded builds (3.14t and 3.15t). Older Pythons aren't supported.
- **Platforms:** macOS, Linux and Windows. Windows has no `fork()`, so the fork guarantees are Unix-only; threads and processes are safe everywhere. On Windows a cache directory can't be deleted while any process has the cache open, and excluding cache directories from real-time antivirus scanning can make them faster.
- **Wheels:** macOS on Apple Silicon, Linux (glibc) on x86_64 and aarch64, and Windows on x86_64, for each of those four Pythons. Anywhere else pip builds from source, which needs the latest stable Rust toolchain.
- **Dependencies:** none at runtime. SQLite is bundled and compiled in, so your system's SQLite version doesn't matter.
- **Upgrading:** before 1.0, a release that changes the on-disk format bumps the minor version and says so first in its changelog entry. A release that meets a cache in another format opens a new one in a subdirectory named for its format (`diskcache-oxide-format-3`) and logs a warning, so old and new workers run side by side; `remove_old_formats()` removes the old cache once the old workers are gone. A release that keeps the format keeps your caches warm.

[Getting started](https://mkennedy.codes/docs/diskcache-oxide/user-guide/getting-started.html) walks through the everyday operations and what changes once several processes share a cache.

## Why

- **[Fast.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/performance.html)** A cached `get` takes about 0.75 µs, less than half of diskcache's time, which is cheap enough to call from the event loop. With 8 threads at a 99:1 read/write mix on a GIL build, diskcache's per-thread connections pile into each other and its throughput falls to a trickle, while oxide's stays flat, 16× ahead.
- **[Safe for threads, processes and `fork()`](https://mkennedy.codes/docs/diskcache-oxide/user-guide/concurrency-and-forks.html)**, including free-threaded Python (3.14t, 3.15t). A fork copies whatever SQLite lock a thread held at that instant, and in diskcache a child born that way stalls on its first write for up to a minute. Here a fork barrier makes sure no child is born holding half a transaction.
- **[No pickle by default.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/serialization.html)** Values are stored as msgpack (or JSON), with exact type tags for tuples, sets, datetimes, dates, Decimals, big ints and NaN. You register your own types, and a library's, once (`register_type(ObjectId, name='bson.ObjectId', encode=str, decode=ObjectId)`; a dataclass needs only a name), and they're read back only where the name is registered. `pickle=True` is opt-in, and a cache without it refuses to unpickle anything it finds, however it got there.
- **[Correct under pressure.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/divergences.html)** `clear()` keeps writes that land while it runs, a read during an overwrite never misses, and a queue never hands one worker another queue's items. Running one test suite against both libraries turned up 19 diskcache bugs, each with a standalone repro in the repository's [upstream/](https://github.com/mikeckennedy/diskcache-oxide/tree/main/upstream) folder, and each fixed here.
- **[More than get and set.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/coordination.html)** `memoize` caches a function's results. Tags group entries so `evict(tag)` removes them in one call. `push` and `pull` make a [work queue](https://mkennedy.codes/docs/diskcache-oxide/recipes/work-queues.html) that hands each item to exactly one worker. Locks, semaphores and rate limits hold across every process on the machine.
- **[Bounded memory and predictable disk use.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/storage-and-wal.html)** Each cache directory uses at most `max_connections` SQLite connections, each with a page cache of `sqlite_cache_size` pages (1 MiB by default). The write-ahead log trims itself back to `wal_size_limit / 2` (32 MiB by default) whenever it resets, and `checkpoint()` truncates it on demand. For a cache that must never drop a live entry, `eviction_policy='none'` turns eviction off.
- **[Sharding for many writers.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/fanoutcache.html)** A plain cache has one write lock per file, like a shop with one checkout lane. `FanoutCache` spreads keys over several databases, so eight worker processes that write often keep their rate instead of queueing.
- **[Transactions.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/transactions.html)** `with cache.transact() as tx:` lands several writes together, or none of them.
- **[A real off switch, and a registry.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/null-cache.html)** `NullCache` turns caching off without changing call sites, and [`Caches`](https://mkennedy.codes/docs/diskcache-oxide/user-guide/caches.html) names an app's caches, opens each on first use, and hands out a `NullCache` while one can't open.
- **[A cache that can't take a request down.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/getting-started.html#a-cache-that-cant-take-a-request-down)** `durable=True` answers an outage as a miss and logs it, so a full disk at 3 a.m. costs speed, not requests.
- Type hints are included (`py.typed`), with `CacheLike` for code that accepts any cache, and an [errors page](https://mkennedy.codes/docs/diskcache-oxide/user-guide/errors.html) says what each exception means and what to do about it.

## Switching from diskcache

diskcache-oxide is API compatible with diskcache 5.6.3's `Cache`, and deliberately not data compatible. For most apps the switch is the import line:

```python
# from diskcache import Cache, Timeout
from diskcache_oxide import Cache, Timeout
```

What you'll notice afterwards:

- **The cache starts cold.** Data lives in `diskcache-oxide.sqlite`, never in diskcache's `cache.db`, and the old database is never opened or converted. The two can share a directory without seeing each other's data.
- **Values that can't round-trip exactly raise `TypeError` at `set()`**, unless the type is registered with `register_type()` or that cache was opened with `pickle=True`. This is the change most apps hit first.
- **Keys and tags are never pickled.** They can be `str`, `bytes`, `int`, `float`, `bool`, `None` or tuples of those, compared by exact type, so `1`, `1.0` and `True` are three keys.
- **Some arguments are keyword-only:** `retry=` and `tag=` everywhere, and `FanoutCache`'s `shards=`, so `FanoutCache(path, 4)` is a 4-second timeout here, not 4 shards. `directory` is required, and `size_limit` must be at least 1 MiB.
- **`Timeout` is a `TimeoutError`**, and `bool(cache)` is always True.

[Switching from python-diskcache](https://mkennedy.codes/docs/diskcache-oxide/user-guide/switching-from-diskcache.html) has the full comparison table, and [Divergences](https://mkennedy.codes/docs/diskcache-oxide/user-guide/divergences.html) gives the reason for each difference.

## Where it's going

1. **A better diskcache:** done. diskcache's `Cache` and `FanoutCache`, safe across processes and forks, faster, and fixed where diskcache goes wrong.
2. **The complete local caching toolkit:** mostly done. Keys of every native type, `memoize`, tags, work queues, and locks and throttles that hold across every process on the machine are all in. Next come a Django cache backend, free-threaded scaling and `AsyncCache`. `Deque` and `Index` will follow if people ask for them.
3. **A cache server:** the same Rust engine served over the network, a Redis-style central cache with SQLite underneath.

Least-recently-used and least-frequently-used eviction aren't planned: diskcache implements them by writing to the database on every `get`, and reads that never write are what let many processes read at once. Custom `disk=` classes aren't planned either, because `serializer=` and `register_type()` cover what they were for. [Vision and roadmap](https://mkennedy.codes/docs/diskcache-oxide/user-guide/roadmap.html) has the detail.

## Shutting down

Close every cache when the process shuts down, with `cache.close()` in your framework's shutdown hook ([Web workers](https://mkennedy.codes/docs/diskcache-oxide/recipes/web-workers.html) shows FastAPI, Flask, Django and Celery). A process that dies without closing loses nothing, but closing releases the cache's SQLite connections and, when the write-ahead log is large, copies it back into the database and truncates it, waiting at most a second. After a burst of writes (a big fill, or a `clear()`) the log can be much larger than the database while other processes keep reading, so call `cache.checkpoint()` before `close()`: it waits briefly for in-flight reads, copies the log and truncates it, and returns `False` instead of raising if other processes keep it busy. [Storage and WAL](https://mkennedy.codes/docs/diskcache-oxide/user-guide/storage-and-wal.html) covers the options, including `wal_checkpoint="auto"` for long-running apps with heavy write bursts.

## For AI coding agents

The docs publish an [agent skill](https://mkennedy.codes/docs/diskcache-oxide/skill.md) with the rules of the package (what raises, what a `NullCache` does to `add()`, when to use `FanoutCache`), plus [llms.txt](https://mkennedy.codes/docs/diskcache-oxide/llms.txt) and [llms-full.txt](https://mkennedy.codes/docs/diskcache-oxide/llms-full.txt). Install the skill with `npx skills add https://mkennedy.codes/docs/diskcache-oxide/`.

## Learn more

- [User guide](https://mkennedy.codes/docs/diskcache-oxide/user-guide/getting-started.html): a [primer for anyone new to caching](https://mkennedy.codes/docs/diskcache-oxide/user-guide/new-to-caching.html), getting started, serialization, `FanoutCache`, transactions, coordination (locks, semaphores, throttles), concurrency and forks, and storage and the WAL.
- [Recipes](https://mkennedy.codes/docs/diskcache-oxide/recipes/): caching expensive calls, stale-while-revalidate, upstream HTTP responses, invalidating groups (by prefix or by tag), your own types, run-once jobs, rate limiting, counters, work queues, single-use keys, sessions, web workers, asyncio and testing.
- [API reference](https://mkennedy.codes/docs/diskcache-oxide/reference/): every class, method and exception.
- [Errors and what to do about them](https://mkennedy.codes/docs/diskcache-oxide/user-guide/errors.html).
- Questions and bug reports: [GitHub issues](https://github.com/mikeckennedy/diskcache-oxide/issues).

To work on diskcache-oxide itself, see the [repository](https://github.com/mikeckennedy/diskcache-oxide), whose [CLAUDE.md](https://github.com/mikeckennedy/diskcache-oxide/blob/main/CLAUDE.md) has the layout, the build and test commands, and the project's conventions.

## License

MIT. See [LICENSE](https://github.com/mikeckennedy/diskcache-oxide/blob/main/LICENSE).
