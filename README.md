<p align="center">
  <img src="https://mkennedy-shared.nyc3.digitaloceanspaces.com/github/diskcache-oxide-slogan.jpg" alt="diskcache-oxide: Python cache. Rust engine. SQLite storage." width="850">
</p>

# diskcache-oxide

**A shared, durable cache for Python apps, rebuilt in Rust for many cores, many processes and free-threaded Python.**

Every Python web app arrives at the same situation. A query that was fine at launch is now the slowest thing on the page, so you cache its result in a dict. Then gunicorn starts four workers and there are four dicts, each warming up alone and forgetting everything on restart. So you reach for Redis, and now there's a server to deploy, secure and monitor, to remember the answer to one query. [python-diskcache](https://github.com/grantjenks/python-diskcache) showed the third way: a cache that is just a directory, shared by every thread and process on the machine, still there after a restart. diskcache-oxide keeps diskcache's API and rebuilds everything underneath it: a Rust engine, SQLite storage, and the thinnest possible Python layer.

**Docs: <https://mkennedy.codes/docs/diskcache-oxide/>**

```python
from diskcache_oxide import Cache

cache = Cache('/var/cache/my-app')  # every worker opens the same directory

cache.set('episodes:popular', episodes, expire=300)
episodes = cache.get('episodes:popular')

# Only one worker runs the hourly job: add() is atomic across processes.
if cache.add('job:refresh-feeds', os.getpid(), expire=3600):
    refresh_feeds()
```

> **Status: pre-release, version 0.1.0.dev2, not yet on PyPI.** `Cache`, `FanoutCache`, `transact()`, `NullCache` and `Caches` are complete. They pass the conformance suite run against real diskcache, apart from the [documented divergences](https://mkennedy.codes/docs/diskcache-oxide/user-guide/divergences.html), and a 24-hour soak that threw processes, threads, forks and `SIGKILL` at one directory. Two gates remain: a whole-codebase sanitizer pass and a fresh soak on the final commit. Then it goes into production at Talk Python, and `0.1.0` goes to PyPI once it has run there. The name on PyPI holds a `0.0.1` placeholder with no code; until `0.1.0`, install from GitHub (needs a Rust toolchain): `uv pip install git+https://github.com/mikeckennedy/diskcache-oxide`.

## Why

- **[Fast.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/performance.html)** A cached `get` is about 0.75 µs, 0.42× diskcache's latency, cheap enough to call from the event loop. With 8 threads at a 99:1 read/write mix on a GIL build, diskcache's per-thread connections pile into each other and its throughput falls to a trickle; oxide's stays flat, 16× ahead.
- **[Safe for threads, processes and `fork()`](https://mkennedy.codes/docs/diskcache-oxide/user-guide/concurrency-and-forks.html)**, including free-threaded Python (3.14t, 3.15t). A fork copies whatever SQLite lock a thread held at that instant, and in diskcache a child born that way stalls on its first write for up to a minute. Here a fork barrier makes sure no child is born holding half a transaction.
- **[No pickle by default.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/serialization.html)** Values are stored as msgpack (or JSON), with exact type tags for tuples, sets, datetimes, big ints and NaN. `pickle=True` is opt-in, and a cache without it refuses to unpickle anything it finds, however it got there.
- **[Correct under pressure.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/divergences.html)** `clear()` keeps writes that land while it runs, `expire()` removes every expired entry, and a read during an overwrite never misses. Each is a diskcache bug, found by running one test suite against both libraries, with a repro in [upstream/](upstream/README.md).
- **[Bounded memory and predictable disk use.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/storage-and-wal.html)** At most `max_connections` SQLite connections per cache directory, each with a page cache of `sqlite_cache_size` pages (1 MiB by default). The write-ahead log trims itself back to `wal_size_limit / 2` (32 MiB by default) whenever it resets, and `checkpoint()` truncates it on demand.
- **[Sharding for many writers.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/fanoutcache.html)** A plain cache has one write lock per file, one checkout lane. `FanoutCache` spreads keys over several databases, so eight worker processes that write often keep their rate instead of queueing.
- **[Transactions.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/transactions.html)** `with cache.transact() as tx:` lands several writes together, or none of them.
- **[A real off switch, and a registry.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/null-cache.html)** `NullCache` turns caching off without changing call sites, and [`Caches`](https://mkennedy.codes/docs/diskcache-oxide/user-guide/caches.html) names an app's caches, opens them on first use, and hands out a `NullCache` while one can't open.
- **[A cache that can't take a request down.](https://mkennedy.codes/docs/diskcache-oxide/user-guide/getting-started.html#a-cache-that-cant-take-a-request-down)** `durable=True` answers an outage as a miss and logs it, so a full disk at 3 a.m. costs speed, not requests.
- Type hints included (`py.typed`), with `CacheLike` for code that accepts any cache, and an [errors page](https://mkennedy.codes/docs/diskcache-oxide/user-guide/errors.html) that says what each exception means and what to do.

## Where it's going

1. **A better diskcache** (now): the `Cache` above, plus `FanoutCache` and `transact()` from the next stage.
2. **The complete local caching toolkit:** free-threaded scaling, `AsyncCache`, `memoize`, tags, `Deque`/`Index`, and recipes like locks and throttles that are atomic across every process on the machine.
3. **A cache server:** the same Rust engine served over the network, a Redis-style central cache with SQLite underneath.

See [Vision and roadmap](https://mkennedy.codes/docs/diskcache-oxide/user-guide/roadmap.html) in the docs.

## Shutting down

Call `cache.checkpoint()` at shutdown if you can, though it isn't required. With readers in other processes, a burst of writes (a big fill, or a `clear()`) can leave SQLite's write-ahead log much larger than the database, and SQLite only shrinks it when it gets a moment with no readers. `checkpoint()` makes that moment: it waits briefly for in-flight reads, copies the log into the database, and truncates it. It returns `False` instead of raising if other processes keep it busy.

`close()` does the same by default, but only when the log is large (over half of `wal_size_limit`), and waits at most a second, so closing stays cheap. Pass `close(checkpoint=False)` to skip it. Long-running apps with heavy write bursts can also call `checkpoint()` from a maintenance job, or opt in to `Cache(..., wal_checkpoint="auto")` (see D-024 in [tests/DIVERGENCES.md](tests/DIVERGENCES.md)). [Storage and WAL](https://mkennedy.codes/docs/diskcache-oxide/user-guide/storage-and-wal.html) in the docs has the details.

## For AI coding agents

The docs publish an [agent skill](https://mkennedy.codes/docs/diskcache-oxide/skill.md) with the rules of the package (what raises, what a `NullCache` does to `add()`, when to use `FanoutCache`), plus [llms.txt](https://mkennedy.codes/docs/diskcache-oxide/llms.txt) and [llms-full.txt](https://mkennedy.codes/docs/diskcache-oxide/llms-full.txt). Install the skill with `npx skills add https://mkennedy.codes/docs/diskcache-oxide/`.

## Where things are

- [The docs](https://mkennedy.codes/docs/diskcache-oxide/): the user guide, recipes and API reference.
- [plans/00-roadmap.md](plans/00-roadmap.md): the phases and what each one delivers.
- [plans/04-design.md](plans/04-design.md): the design.
- [tests/DIVERGENCES.md](tests/DIVERGENCES.md): every deliberate difference from diskcache, and why.
- [upstream/README.md](upstream/README.md): diskcache bugs found along the way, with standalone repros.
- `scripts/check.sh`: everything CI would run, locally, on Python 3.14, 3.14t, 3.15 and 3.15t.
- `make docs`: builds the docs site into `site/`.
