<p align="center">
  <img src="https://mkennedy-shared.nyc3.digitaloceanspaces.com/github/diskcache-oxide-slogan.jpg" alt="diskcache-oxide: Python cache. Rust engine. SQLite storage." width="850">
</p>

# diskcache-oxide

**A shared, durable cache for Python apps, rebuilt in Rust for many cores, many processes and free-threaded Python.**

Almost every Python web app ends up needing a cache that all of its worker processes can see. An in-memory dict isn't shared and dies on restart. Redis is one more server to run. [python-diskcache](https://github.com/grantjenks/python-diskcache) made the middle path popular: a cache that is just a directory, shared by every thread and process on the machine. diskcache-oxide keeps diskcache's API and rebuilds everything underneath: a Rust engine, SQLite storage, and the thinnest possible Python layer.

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

> **Status: pre-release, not yet published.** The core `Cache` works, and it passes the conformance suite run against real diskcache, apart from the documented divergences. It's now in a deep concurrency review and 24-hour soak runs, then production at Talk Python, before a first release.

## Why

- **Fast.** A cached `get` is about 0.75 µs, 0.42× diskcache's latency. With 8 threads at a 99:1 read/write mix, it handles 16× diskcache's throughput on a GIL build.
- **Safe for threads, processes and `fork()`**, including free-threaded Python (3.14t, 3.15t). A child forked while another thread was writing can always write; in diskcache it can stall for a minute.
- **No pickle by default.** Values are stored as msgpack (or JSON), with exact type tags for tuples, sets, datetimes, big ints and NaN. `pickle=True` is opt-in, and a cache without it refuses to unpickle anything it finds.
- **Correct under pressure.** `clear()` keeps writes that land while it runs, `expire()` removes every expired entry, and a read during an overwrite never misses. Each is a diskcache bug with a repro in [upstream/](upstream/README.md).
- **Bounded memory:** at most `max_connections` SQLite connections per cache directory, each with a page cache of `sqlite_cache_size` pages (1 MiB by default).
- **Predictable disk use:** the write-ahead log trims itself back to `wal_size_limit / 2` (32 MiB by default) whenever it resets, and `checkpoint()` truncates it on demand.
- Type hints included (`py.typed`), and a `NullCache` that turns caching off without changing call sites.

## Where it's going

1. **A better diskcache** (now): the `Cache` above.
2. **The complete local caching toolkit:** free-threaded scaling, `FanoutCache`, `AsyncCache`, `memoize`, tags, `transact()`, `Deque`/`Index`, and recipes like locks and throttles that are atomic across every process on the machine.
3. **A cache server:** the same Rust engine served over the network, a Redis-style central cache with SQLite underneath.

See [Vision and roadmap](https://mkennedy.codes/docs/diskcache-oxide/user-guide/roadmap.html) in the docs.

## Shutting down

Call `cache.checkpoint()` at shutdown if you can, though it isn't required. With readers in other processes, a burst of writes (a big fill, or a `clear()`) can leave SQLite's write-ahead log much larger than the database, and SQLite only shrinks it when it gets a moment with no readers. `checkpoint()` makes that moment: it waits briefly for in-flight reads, copies the log into the database, and truncates it. It returns `False` instead of raising if other processes keep it busy.

`close()` does the same by default, but only when the log is large (over half of `wal_size_limit`), and waits at most a second, so closing stays cheap. Pass `close(checkpoint=False)` to skip it. Long-running apps with heavy write bursts can also call `checkpoint()` from a maintenance job, or opt in to `Cache(..., wal_checkpoint="auto")` (see D-024 in [tests/DIVERGENCES.md](tests/DIVERGENCES.md)).

## Where things are

- [plans/00-roadmap.md](plans/00-roadmap.md): the phases and what each one delivers.
- [plans/04-design.md](plans/04-design.md): the design.
- [tests/DIVERGENCES.md](tests/DIVERGENCES.md): every deliberate difference from diskcache, and why.
- [upstream/README.md](upstream/README.md): diskcache bugs found along the way, with standalone repros.
- `scripts/check.sh`: everything CI would run, locally, on Python 3.14, 3.14t, 3.15 and 3.15t.
- `make docs`: builds the docs site into `site/`.
