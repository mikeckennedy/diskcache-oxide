# Performance

A microsecond is nothing until you multiply it. Web apps call their cache on almost every request, often straight from the asyncio event loop, where every microsecond the cache spends is a microsecond every other request on that loop waits. So diskcache-oxide's performance work starts with read latency, then throughput under many threads, then predictable resource use. Correctness always comes first: every speedup has to keep the conformance suite green, or it doesn't ship. The numbers below are the ones that survived.


# The numbers

Measured with `bench/gate.py`, which runs diskcache 5.6.3 and diskcache-oxide in the same session on the same machine, in four interleaved rounds. These numbers come from Linux (Ubuntu 26.04 LTS) in a container on an Apple Silicon Mac. Both libraries ran in the same container, so the ratios compare like with like, but the absolute numbers on an x86-64 server will differ. Latencies are p50 in microseconds. Ratios are oxide's time divided by diskcache's, so lower is better.

| Workload | 3.14 | 3.14t (free-threaded) |
|----|----|----|
| [get](../reference/Cache.get.md#diskcache_oxide.Cache.get), small value, hit | **0.76 µs** (0.42×) | **0.75 µs** (0.46×) |
| [get](../reference/Cache.get.md#diskcache_oxide.Cache.get), miss | **0.65 µs** (0.45×) | **0.62 µs** (0.46×) |
| [get](../reference/Cache.get.md#diskcache_oxide.Cache.get), small dict | **1.18 µs** (0.47×) | **1.26 µs** (0.54×) |
| [get](../reference/Cache.get.md#diskcache_oxide.Cache.get), random keys over a large database | **2.21 µs** (0.65×) | **2.41 µs** (0.79×) |
| [set](../reference/Cache.set.md#diskcache_oxide.Cache.set), small value | **22.6 µs** (0.77×) | **24.0 µs** (0.82×) |

Threaded throughput, every thread sharing one [Cache](../reference/Cache.md#diskcache_oxide.Cache), in thousands of operations per second at 1/2/4/8 threads (ratios are oxide's throughput over diskcache's, so higher is better):

| Mix | 3.14 | 3.14t (free-threaded) |
|----|----|----|
| 99:1 reads to writes | **750 / 745 / 723 / 723** (2.0-16.5×) | **701 / 639 / 680 / 729** (1.7-1.8×) |
| 1,000:1 reads to writes | **900 / 886 / 876 / 875** (2.1-18.9×) | **903 / 680 / 1,181 / 1,416** (1.25-2.2×) |

Two things stand out:

- **On GIL builds, diskcache collapses as threads are added and oxide doesn't.** At 99:1, diskcache drops from about 375k to 44k operations per second going from 1 to 8 threads; oxide stays flat around 720-750k. diskcache's per-thread connections end up fighting over the GIL and SQLite's locks.
- **On free-threaded builds, oxide is faster at every thread count, but it doesn't scale yet.** 8 threads reach about 1.4M operations per second at 1,000:1, only 1.6× one thread. That's the next target, below.

Run them yourself with `python -m bench.gate`. Numbers vary by machine; the ratios are what the gate enforces.


# Where the speed comes from

- **Rust from the call to the disk.** A [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) goes from Python straight into compiled Rust: one prepared SQLite statement, decode, done. There's no Python wrapper in between.
- **Statements prepared once.** Each pooled connection caches its prepared statements, so a lookup never recompiles SQL.
- **The clock is read in Rust.** Checking expiry doesn't call back into Python's `time.time()`.
- **Fast codecs.** msgpack and tagged JSON are encoded and decoded in Rust, building Python objects directly.
- **Reads never write.** Least-recently-stored eviction means a [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) never updates anything, so any number of readers proceed in parallel.
- **A smarter write lock.** Writers in one process queue on an in-process lock per database file, instead of every thread retrying against SQLite's busy handler.
- **SQLite tuned for it.** The bundled SQLite is built with its global memory statistics turned off, which removes a process-wide mutex from every allocation: 2-10% more threaded throughput.


# Prefix scans at scale

diskcache's [iterkeys()](../reference/Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys) gets slower faster than the cache grows: about 1.7 seconds at 100,000 keys, and over 25 seconds at 400,000. diskcache-oxide's `iterkeys(prefix=...)` is an indexed range scan, paged in batches so no read transaction is held between them. **400,000 keys scan in about 10 ms.**


# Memory that stays flat

Memory is bounded by configuration, not by how many threads touch the cache: at most `max_connections` connections per database file (default 8), each with a 1 MiB page cache. A [`FanoutCache`](fanoutcache.md) defaults to two per shard for the whole root, about 16 MiB at 8 shards. A separate memory-instrumented build runs `bench/memory.py` and the memory tests under heavy load to hold that bound. See [Storage and WAL](storage-and-wal.md).


# Known slow spots

Two oversubscribed cases measure slower than before the SQLite mutex spin and the connection-slot spin went in. Both are known, and neither is in the path of a web app running a few threads per process.

- **A plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) with far more threads than cores**, all sharing one handle: 7% slower on an M-series Mac at 36 threads, and 5% in a Linux VM at 32. Faster hand-off lets more threads into SQLite at once.
- **A [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) at `max_connections=32` with 8 threads, on macOS only:** 5-12% slower. The same case on Linux is 41-56% faster.


# Next: free-threaded scaling

Profiling a cached [get](../reference/Cache.get.md#diskcache_oxide.Cache.get) shows that about 60% of its time is one system call: SQLite taking its shared-memory read lock on the `-shm` file, under a process-wide mutex. It's the biggest single cost on every read, and it's also what keeps free-threaded throughput from scaling. The next stage goes after it, along with:

- a per-process lock-caching layer under SQLite, or longer-lived read snapshots;
- thread-affine connections;
- cheaper reference counting on shared [Cache](../reference/Cache.md#diskcache_oxide.Cache) objects in free-threaded builds;
- Making threads that share one handle scale. [`FanoutCache`](fanoutcache.md) is built and removes the multi-process write-lock ceiling, but at its default settings it doesn't lift this read ceiling, so the shared per-handle path is being worked on.

Past that, the [network server](roadmap.md) can go further still: as the only owner of its files, it can use SQLite's exclusive locking mode, which keeps that lock in ordinary memory. Measured reads drop from 1.18 µs to 0.69 µs.
