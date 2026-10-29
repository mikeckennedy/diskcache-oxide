# Divergences from diskcache 5.6.3

Every intentional behavioral difference between diskcache-oxide and diskcache 5.6.3. Each entry says whether it is **test-visible** (a conformance test carries `@pytest.mark.diverges(ref=...)` for it, or a `backend_only("oxide")` test asserts the new behavior) or a **note** (hidden by the test fixtures, but users should know about it).

A divergence is a deliberate decision. It is never a way to skip a failing test.

## D-001 Unrepresentable values raise TypeError by default

- Kind: test-visible
- diskcache: pickles every value, so anything picklable round-trips.
- oxide: stores `str`/`bytes` raw and structured values as JSON (with tags) or msgpack. With the default `pickle=False`, a value the chosen format can't represent exactly raises `TypeError` at `set`. Passing `pickle=True` restores the pickle fallback for that cache. On the read side, a `pickle=False` handle never unpickles: reading a pickled row written by a `pickle=True` peer raises `UnsafeValueError` (a `TypeError` subclass) and leaves the row in place.
- Why: unpickling runs arbitrary code from whatever is in the cache directory, and silently pickling hides values that weren't meant to be cached.
- Tests: `test_values.py`

## D-002 Database filename

- Kind: note
- diskcache: `cache.db`
- oxide: `diskcache-oxide.sqlite`
- Why: the two formats can share a directory without ever misreading each other, and a cutover never opens a stale diskcache database.

## D-003 Clock control

- Kind: note
- diskcache: reads `time.time()`, so tests advance time by monkeypatching it.
- oxide: reads the clock natively. Tests pass `clock=` (e.g. `clock=lambda: time.time()` to follow a monkeypatch).
- Why: calling into Python for the time on every operation would cost the hot path.

## D-004 String keys only (until Phase 6a)

- Kind: test-visible
- diskcache: accepts any picklable key (int, bytes, float, tuple, ...).
- oxide: accepts `str` keys only for now; any other key type raises `TypeError`. Keys are stored in a type-tagged column so other types can be added without a migration.
- Tests: `test_basic.py`

## D-005 Minimum size_limit

- Kind: test-visible
- diskcache: a `size_limit` below the empty-database volume (about 32 KB) silently culls everything on every `set`, including the value just stored (`set` returns True and the cache stays empty).
- oxide: `size_limit` below `MIN_SIZE_LIMIT` (1 MiB) raises `ValueError`.
- Tests: `test_eviction.py`

## D-006 Empty-database volume

- Kind: note
- diskcache and oxide report different `volume()` values for an empty cache, so an absolute `size_limit` leaves a slightly different amount of room. Tests size their limits relative to the empty volume.

## D-007 Timeout is a TimeoutError

- Kind: test-visible
- diskcache: `Timeout` subclasses only `Exception`.
- oxide: `Timeout` subclasses `TimeoutError`. Code that catches `Timeout` or `Exception` behaves the same.
- Tests: `test_basic.py`

## D-008 A top-level NaN round-trips

- Kind: test-visible
- diskcache: `set('k', float('nan'))` returns True and the key exists, but `get('k')` returns `None`, ignoring any default. NaN nested in a container survives. Found by the Phase 0 value tests.
- oxide: a top-level NaN round-trips like any other float.
- Why: this is a diskcache bug, not behavior anyone depends on.
- Tests: `test_values.py` (the `float-nan` round-trip cases are strict xfails on diskcache)

## D-009 A Cache is always truthy

- Kind: test-visible
- diskcache: truthiness comes from `__len__`, so an open, usable, empty cache is falsy. Talk Python wraps every cache to work around this.
- oxide: `bool(Cache)` is always True, and `Cache.enabled` is True. `bool(NullCache)` is False and `NullCache.enabled` is False, so `if cache:` means "caching is on".
- Tests: `test_basic.py`

## D-010 expire() removes every expired entry

- Kind: test-visible
- diskcache: `expire()` works in chunks of 100, and when expired entries share one expire time it stops after the first chunk (with 250 entries sharing a time, one call removes 100). Entries with distinct expire times are all removed. Found by the Phase 0 maintenance tests.
- oxide: one `expire()` call removes every entry that has expired, still in chunks so writers can get in between them.
- Why: this is a diskcache bug. Talk Python's hourly sweep would silently leave expired rows behind.
- Tests: `test_maintenance.py`

## D-011 clear() removes only what existed when it started

- Kind: note (to be pinned with an oxide test in Phase 2)
- diskcache: `clear()` keeps deleting until the cache is empty, including entries written while it runs. In a repro with 50k entries and a concurrent writer, `clear()` removed 133k rows: 76k writes that had already succeeded were deleted and only 1 survived. Its running time grows with the write rate. Found by the Phase 0 locking tests (repro: `upstream/repros/clear_deletes_concurrent_writes.py`).
- oxide: `clear()` removes the entries stored before it started (bounded by a `store_seq` snapshot) and always finishes. Writes that land while it runs, including overwrites of existing keys, survive.
- Why: a maintenance call must be bounded, and it must not silently discard writes that callers saw succeed.

## D-012 A forked child can write even if another thread was writing at the fork

- Kind: test-visible
- diskcache: when a process forks while another of its threads is inside a write transaction on the same `Cache`, the child's reads work but its first write raises `Timeout` after the cache's `timeout` (60 s by default), even though the parent thread commits right after the fork. The child inherits SQLite lock state for a lock nobody in the child will release. Under a get/set hammer this hits about 20-50% of forks; a reads-only hammer never triggers it. The parent is unaffected. Found by the Phase 0 review fixes.
- oxide: a forked child can always read and write, whatever other threads were doing when the fork happened. How this is done is a Phase 1 storage decision. Discarding the inherited connections isn't enough on its own, because SQLite's per-file lock bookkeeping is shared by every connection in a process and the child inherits it. The leading approach is a before-fork hook (`os.register_at_fork`) that waits for in-flight transactions to finish and holds off new ones while the fork happens.
- Why: pre-fork servers (gunicorn, Celery prefork) fork while background threads may be using the cache, and a 60 s stall in a fresh worker is hard to diagnose.
- Tests: `test_processes.py` (`test_fork_while_another_thread_holds_a_write_transaction` is a strict xfail on diskcache; `test_fork_while_another_thread_writes_the_cache` is oxide-only). The barrier must cover every `Cache` in the process, not just the one the fork goes through (upstream #325): `test_fork_while_another_thread_writes_through_a_second_cache` (oxide and community) hammers writes through a second `Cache` on the same directory while the main thread forks with the first. On diskcache a child's write timed out in 13/25 runs on 3.14 and 15/25 on 3.14t (two forks per run); one child on 3.14 hung until it was killed instead.

## D-013 A missing value file is a miss, and the row is removed

- Kind: test-visible
- diskcache: if a large value's file disappears from disk, `get` returns the default, but the row stays: `in` is True and `len` counts it, until an explicit `delete` or `check()`. The diskcache-community fork deliberately keeps this behavior.
- oxide: `get` returns the default, and the orphaned row is removed. Because reads must never block on or wait for the write lock, the removal is opportunistic: `get` tries it without waiting, never raises if the lock is busy, and leaves any row it couldn't remove to the next write's cleanup.
- Why: a key that is "present" but can never be read breaks `in`-then-`get` patterns.
- Tests: `test_maintenance.py`

## D-014 Bool expire and timeout are rejected

- Kind: test-visible
- diskcache: `bool` is an `int` subclass, so `expire=True` is silently a 1-second TTL, `expire=False` is 0 (immediately expired), and `timeout=True` is accepted. Upstream PR #376 proposes rejecting these.
- oxide: `expire=True`/`False` (on `set`, `add`, `touch`) and `timeout=True`/`False` raise `TypeError`.
- Why: a bool here is almost always a mistake (e.g. `read=True` passed in the wrong position), and quietly turning it into a number hides that mistake.
- Tests: `test_expiry.py`, `test_basic.py`

## D-015 New cache directories are owner-only

- Kind: test-visible
- diskcache: new cache directories follow the process umask (typically 0755). Upstream PR #375 keeps the umask; the diskcache-community fork uses 0700.
- oxide: a cache directory that diskcache-oxide creates, and any subdirectories it creates for value files, are 0700. Intermediate parents follow the umask, like `os.makedirs`. An existing directory's permissions are never changed.
- Why: a cache directory is a security boundary. Anyone who can write to it can poison the cache, and its contents (e.g. keys containing IP addresses) can be private.
- Tests: `test_basic.py`

## D-016 A key that's present the whole time never reads as missing while its value is overwritten

- Kind: test-visible
- diskcache: while one process overwrites a spilled (file-backed) value and other processes `get` it, an occasional read returns the default, though the key is never deleted. No torn values, `None` or exceptions were seen, and afterwards the key, `len` and the value-file count are all correct. Measured with `test_processes.py`'s Test A: the default variant (4 reader processes, 500 overwrites of 100-400 KB values) failed in 5/20 runs on 3.14 and 3/20 on 3.14t (a lighter 3-reader, 150-overwrite version failed in 1/75 and 5/75); the heavy variant (6 readers, 3000 overwrites) failed in 10/10 runs on both. The standalone repro sees about 40 misses in 325k reads, every run. The diskcache-community fork has the same bug. Found by the Phase 0.5 tests (upstream #294, and #258 for threads).
- oxide: a read of a spilled value that finds its file missing reads the row again and retries if the file reference or `store_seq` changed, so it always returns a whole value, old or new. It's a miss only when the row still points at the same missing file (D-013), and the D-013 cleanup is a compare-and-delete, so a reader can never delete a live row a writer just replaced.
- Why: `get` with a default is how callers decide to recompute a value. Spurious misses on hot, large values cause needless recomputes, and a reader that "cleans up" after one would lose the live value.
- Tests: `test_processes.py` (`test_reading_a_spilled_value_while_it_is_overwritten_never_misses` and its `slow` heavy variant are oxide-only, because the failure on diskcache and the fork is random)

## D-017 Connections and fds are bounded by the pool

- Kind: test-visible
- diskcache: one shared `Cache` opens a SQLite connection for every thread that uses it, and each holds the database and its WAL open. With 64 threads held after one `get` and one `set` each, 131 fds are open inside the cache directory (2 × (threads + 1) + 1 for the shared `-shm`), on 3.14 and 3.14t. A server with many threads can run into the fd limit (upstream #295, #336).
- oxide: connections come from a bounded pool, so the fd count inside the cache directory is at most 2 × pool size + 1, however many threads are alive (`OXIDE_FD_BUDGET` in `backends.py`). The pool is per database file and shared by every handle on it. Its size is the largest `max_connections` any live handle asked for (default 8; 04-design.md section 7). A nested operation inside a held transaction may open over the cap, and a forked child deliberately leaks the connections it inherited, so fd tests that combine forks must allow for that.
- Why: the number of open fds should depend on configuration, not on how many threads happened to touch the cache.
- Tests: `test_threads.py` (`test_peak_fds_bounded_by_the_pool` is a strict xfail on diskcache; `test_peak_fds_within_the_backend_budget` pins each adapter's `fd_budget`)

## D-018 close() closes every connection

- Kind: test-visible
- diskcache: `close()` closes only the calling thread's connection. After 64 threads have used a shared `Cache` and exited, `close()` leaves 130 fds open inside the cache directory until a gc pass finalizes the connections, and each one then reports `ResourceWarning: unclosed database` (64 warnings, on 3.14 and 3.14t).
- oxide: connections belong to a per-file `Store` shared by every handle on that file. When the last open handle calls `close()`, every connection is closed, whichever threads used it. No fd inside the cache directory stays open (apart from the one fd the `Store` keeps on the database file while any handle is alive; the tests count after the last close), and nothing warns at finalization.
- Why: after `close()` the directory must be safe to delete or move, and leaked connections hide in tests behind `ResourceWarning` noise.
- Tests: `test_threads.py` (`test_close_releases_every_fd` and `test_threads_then_close_emit_no_resource_warning` are strict xfails on diskcache)

## D-019 Opening with unchanged settings never writes

- Kind: test-visible
- diskcache: opening an existing cache with the same settings commits a write transaction (from outside it looks like the Settings rows being rewritten with the same values), so a peer connection's `PRAGMA data_version` goes from 2 to 3 on open, even when every operation afterwards is a read. The main database file's bytes don't change, so only `data_version` catches it.
- oxide: opening with unchanged settings, then `get`, `in`, `len`, `iterkeys` and `volume`, never write: a peer's `data_version` is unchanged, and so are the main database file's hash and mtime.
- Why: a read-mostly service (or a read-only replica of the directory) shouldn't take the write lock or dirty the WAL just by opening its cache.
- Tests: `test_basic.py` (`test_open_and_read_never_write` is a strict xfail on diskcache)

## D-020 Forking inside your own transaction fails fast

- Kind: note (pinned with a test when `transact()` lands in Phase 6b)
- diskcache: forking from a thread that is inside its own `transact()` leaves the child stuck behind a write lock nobody will release. Its writes wait out the full timeout (60 s by default), and the parent gets no warning.
- oxide: CPython can't be stopped from forking, and SQLite's inherited lock table means the child can never write to that database. So the child's inherited transaction is poisoned (operations inside it and its exit raise `RuntimeError`, and nothing commits), and its writes to that database raise `RuntimeError` at once instead of timing out. Reads, and other cache directories, keep working. The parent gets a `RuntimeWarning` when it forks inside a transaction.
- Why: a clear error at the call site beats a 60-second stall in a fresh worker.

## D-021 directory is required

- Kind: note
- diskcache: `Cache()` with no directory (or `directory=None`) creates a temporary directory.
- oxide: `directory` is a required argument.
- Why: a cache directory is a security boundary (D-015) and shared state across processes. An implicit temporary directory is almost never what a multi-process app wants, and it hides where the data lives.

## D-022 No disk= or eviction_policy= (yet)

- Kind: note
- diskcache: accepts a `disk` class (also positionally, as the third argument) and `eviction_policy` (`least-recently-stored`, `least-recently-used`, `least-frequently-used`, `none`).
- oxide: serialization is chosen with `serializer=` and `pickle=` instead of a `Disk` class, and there's no plan to support custom `Disk` subclasses. `eviction_policy` is always least-recently-stored until the other policies arrive in Phase 6a. Only `directory` and `timeout` can be passed positionally.
- Why: a pluggable Python `Disk` would put Python code on every read and write in the Rust hot path.

## D-023 sqlite_cache_size is a per-process memory budget, 1 MiB by default

- Kind: note
- diskcache: `sqlite_cache_size` (in pages, default 8192, so 32 MiB per connection) is a setting stored in the cache's `Settings` table, and it comes with a 64 MiB `sqlite_mmap_size`. Every thread opens its own connection, so SQLite's memory grows with the thread count.
- oxide: `Cache(..., sqlite_cache_size=pages)` takes the same unit (4 KiB pages) but is a per-process option, not stored in the database. The default is 256 pages (1 MiB). Within one process, all handles on a directory share a pool of at most `max_connections` (default 8) connections, so SQLite's page caches for that directory stay at about `max_connections × sqlite_cache_size` pages (8 MiB by default). A held transaction or a nested operation can open one connection beyond the cap. The largest value any handle asks for applies, as with `max_connections`. There's no `sqlite_mmap_size`. `tests/unit/test_memory.py` and `bench/memory.py` hold and report the bound. They run on a memory-instrumented build, since the shipped one turns SQLite's memory statistics off for speed.
- Why: bounded, predictable memory in long-running web workers. The default was the smallest size that kept hot and skewed reads, writes and the threaded mixes unchanged. It costs about 5% on the worst case (uniform random reads over a database much larger than the cache, the `get_random_wide` benchmark), where it's still about 0.8× diskcache's latency. Raise it for large, uniformly read caches: 2000 pages measured about 17% faster there.

## D-024 The WAL file is kept bounded (wal_size_limit)

- Kind: note
- diskcache: leaves SQLite's WAL to its defaults. With readers in other processes, a burst of writes (a fill, a `clear()`) can't wrap the WAL, so the file grows to fit the burst and never shrinks: WALs of hundreds of MiB to over 1 GiB next to a database of a few MiB.
- oxide: `Cache(..., wal_size_limit=bytes)` (default 64 MiB, at least 1 MiB; per process, not stored; the largest value any handle asks for wins, as with `sqlite_cache_size` in D-023) keeps the `-wal` file bounded:
  - `journal_size_limit` is set to half the limit, so a normally reset WAL shrinks back;
  - `wal_autocheckpoint` is scaled from the limit;
  - after a write, at most every 250 ms or 64 writes, a WAL over the limit gets a forced checkpoint: `PASSIVE`, then `TRUNCATE` within a 2 ms busy window. It backs off for 1 s if busy, and never fails the write.
  - Found by the Phase 2.5 soak; `tests/unit/test_wal.py` holds it, and `python -m bench.wal_burst` measures it.
  - Limitation: the bound is reached eventually, not held at every instant. During sustained write bursts with many readers in other processes, the WAL can overshoot the limit until a forced checkpoint wins. On Linux at the default limit it peaked around 185 MiB, then shrank to 32 MiB. The writer that triggers an attempt can pause for up to about 25 ms (SQLite copying the backlog), at most once a second, and only while the WAL is over the limit. A background maintenance thread can do better.
- Why: a cache's disk use should be predictable. A WAL that only ever grows is a slow disk leak in any multi-process deployment.

# Community fork differences

How [diskcache-community](https://github.com/secondwindoss/diskcache-community) 5.8.1 (`--backend=community`) differs from 5.6.3 on this suite. Where the fork fails a test written against 5.6.3, the test is listed in `KNOWN_COMMUNITY_DIFFERENCES` in `backends.py` and runs as a strict xfail on the community backend only, so the community run is green and a fork release that changes the behavior fails it. `test_registry.py` checks that every D-number listed there appears in this section.

- **D-012 (fork during another thread's write transaction):** fixed in the fork. Its before-fork gate waits for the transaction, so the child can write. The D-012 test passes on community.
- **D-015 (directory permissions):** the fork creates new cache directories as 0700, so `test_new_directory_follows_the_umask` fails on community. That matches oxide's behavior.
- **D-008 (top-level NaN reads back as `None`):** the fork has the same bug. The `float-nan` round-trip cases fail on community, as they do (as strict xfails) on 5.6.3.
- **D-017 (connections bounded by the pool):** the fork also keeps one connection per thread: 131 fds inside the cache directory with 64 threads, the same as 5.6.3, so `test_peak_fds_bounded_by_the_pool` fails on community.
- **D-018 (close() closes every connection):** fixed in the fork. After 64 threads and `close()`, no fds remain inside the cache directory and nothing warns, so both D-018 tests pass on community.
- **D-019 (opening never writes):** fixed in the fork, as its PR #17 ("no writes on open") claims. A peer's `data_version` and the main database file are unchanged, so `test_open_and_read_never_write` passes on community.
- **D-016 (a spilled value read as missing while it's overwritten):** the fork has the same bug, so the Test A tests are oxide-only. The default variant failed on community in 4/20 runs on 3.14 and 9/20 on 3.14t, and the heavy variant in every run tried (4/4 on 3.14t).
- **D-012 across two `Cache` objects (upstream #325):** fixed in the fork. Its fork gate also covers a second `Cache` on the same directory: `test_fork_while_another_thread_writes_through_a_second_cache` passed in 20/20 runs on 3.14 and on 3.14t.
- **Phase 0.5 Tests B, C (idle fork), G and I** pass on the fork as on 5.6.3, including racing creators of a new directory still getting 0700 (D-015). After 6 SIGKILLs of a spill writer (Test I), the fork left 0-3 orphaned value files and 5.6.3 left 0-5 (20 runs each); after 60 kills, 11 and 10.
- Everything else in the suite passes on the fork unchanged.
