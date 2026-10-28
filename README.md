# diskcache-oxide

A clean-room reimplementation of [python-diskcache](https://github.com/grantjenks/python-diskcache) 5.6.3 in Rust (via PyO3), with a minimal Python layer. It aims to be API compatible with diskcache, not data compatible: it uses its own on-disk format, so point it at a fresh directory.

> **Status: pre-release, not yet published.** The core `Cache` works, and it passes the conformance suite run against real diskcache, apart from the documented divergences. It still needs a concurrency review and long soak runs before any production use. `FanoutCache`, `transact()`, tags, eviction policies other than least-recently-stored, `Deque`/`Index` and the recipes come in later phases.

## Why

- Safe for threads and processes, including free-threaded Python (3.14t), and safe across `fork()`.
- Fast: roughly half diskcache's latency on cached reads, and much higher multi-threaded throughput.
- No pickle by default. Values are stored as msgpack (or JSON), with exact type tags for tuples, sets, datetimes and big ints. `pickle=True` is opt-in.
- Bounded memory: at most `max_connections` SQLite connections per cache directory, each with a page cache of `sqlite_cache_size` pages (1 MiB by default).
- Type hints included (`py.typed`), and a `NullCache` that turns caching off without changing call sites.

## Where things are

- [plans/00-roadmap.md](plans/00-roadmap.md): the phases and what each one delivers.
- [plans/04-design.md](plans/04-design.md): the design.
- [tests/DIVERGENCES.md](tests/DIVERGENCES.md): every deliberate difference from diskcache, and why.
- [upstream/README.md](upstream/README.md): diskcache bugs found along the way, with standalone repros.
- `scripts/check.sh`: everything CI would run, locally, on Python 3.14, 3.14t, 3.15 and 3.15t.
