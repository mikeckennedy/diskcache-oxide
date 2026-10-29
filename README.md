<p align="center">
  <img src="https://mkennedy-shared.nyc3.digitaloceanspaces.com/github/diskcache-oxide-slogan.jpg" alt="diskcache-oxide: Python cache. Rust engine. SQLite storage." width="850">
</p>

# diskcache-oxide

A clean-room reimplementation of [python-diskcache](https://github.com/grantjenks/python-diskcache) 5.6.3 in Rust (via PyO3), with a minimal Python layer. It aims to be API compatible with diskcache, not data compatible: it uses its own on-disk format, so point it at a fresh directory.

> **Status: pre-release, not yet published.** The core `Cache` works, and it passes the conformance suite run against real diskcache, apart from the documented divergences. It still needs a concurrency review and long soak runs before any production use. `FanoutCache`, `transact()`, tags, eviction policies other than least-recently-stored, `Deque`/`Index` and the recipes come in later phases.

## Why

- Safe for threads and processes, including free-threaded Python (3.14t), and safe across `fork()`.
- Fast: roughly half diskcache's latency on cached reads, and much higher multi-threaded throughput.
- No pickle by default. Values are stored as msgpack (or JSON), with exact type tags for tuples, sets, datetimes and big ints. `pickle=True` is opt-in.
- Bounded memory: at most `max_connections` SQLite connections per cache directory, each with a page cache of `sqlite_cache_size` pages (1 MiB by default).
- Predictable disk use: the write-ahead log trims itself back to `wal_size_limit / 2` (32 MiB by default) whenever it resets, and `checkpoint()` truncates it on demand.
- Type hints included (`py.typed`), and a `NullCache` that turns caching off without changing call sites.

## Shutting down

Call `cache.checkpoint()` at shutdown if you can, though it isn't required. With readers in other processes, a burst of writes (a big fill, or a `clear()`) can leave SQLite's write-ahead log much larger than the database, and SQLite only shrinks it when it gets a moment with no readers. `checkpoint()` makes that moment: it waits briefly for in-flight reads, copies the log into the database, and truncates it. It returns `False` instead of raising if other processes keep it busy.

`close()` does the same by default, but only when the log is large (over half of `wal_size_limit`), and waits at most a second, so closing stays cheap. Pass `close(checkpoint=False)` to skip it. Long-running apps with heavy write bursts can also call `checkpoint()` from a maintenance job, or opt in to `Cache(..., wal_checkpoint="auto")` (see D-024 in [tests/DIVERGENCES.md](tests/DIVERGENCES.md)).

## Where things are

- [plans/00-roadmap.md](plans/00-roadmap.md): the phases and what each one delivers.
- [plans/04-design.md](plans/04-design.md): the design.
- [tests/DIVERGENCES.md](tests/DIVERGENCES.md): every deliberate difference from diskcache, and why.
- [upstream/README.md](upstream/README.md): diskcache bugs found along the way, with standalone repros.
- `scripts/check.sh`: everything CI would run, locally, on Python 3.14, 3.14t, 3.15 and 3.15t.
