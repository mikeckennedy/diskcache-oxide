# Divergences from diskcache 5.6.3

Every intentional behavioral difference between diskcache-oxide and diskcache 5.6.3. Each entry says whether it is **test-visible** (a conformance test carries `@pytest.mark.diverges(ref=...)` for it, or a `backend_only("oxide")` test asserts the new behavior) or a **note** (hidden by the test fixtures, or pinned only by an oxide unit test, but users should know about it).

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

## D-004 Keys are `str`, `bytes`, `int`, `float`, `bool`, `None` and tuples of those

- Kind: test-visible
- diskcache: accepts any picklable key; `str`, `int`, `float` and `bytes` are stored natively and everything else is pickled.
- oxide: accepts `str`, `bytes`, `int` within 64 bits, `float`, `bool`, `None`, and tuples of those nested up to 32 deep, never pickled. Only the exact types: a subclass (`IntEnum`, `StrEnum`, a `namedtuple`), `bytearray`, `memoryview`, `list`, `frozenset` and an int past 64 bits raise `TypeError`; a NaN raises `ValueError` (diskcache stores a lone NaN key as NULL and never finds it again). Keys are compared by exact type (D-044), with no Unicode normalization. A `str` key is stored as it always was; every other key is a typed key in a byte format of our own (`crates/diskcache-oxide-core/src/key.rs`), which 0.1.2 never reads, so 0.1.2 and 0.2.0 can share a cache during a deploy.
- Why: pickled keys would put pickle on every lookup and make a key's identity depend on pickle's bytes (D-001). A key's stored bytes are its identity and its FanoutCache shard, so they must follow from the value alone.
- Tests: `test_keys.py`, `key::tests`

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
- oxide: a forked child can always read and write, whatever other threads were doing when the fork happened. Discarding the inherited connections isn't enough on its own, because SQLite's per-file lock bookkeeping is shared by every connection in a process and the child inherits it. So a before-fork hook (`os.register_at_fork`) waits for in-flight transactions to finish, holds off new ones while the fork happens, and closes the idle connections so the child doesn't inherit that bookkeeping at all. A child that did ran with no real locks, and once the parent closed, another process's last close could delete the WAL under it and lose the child's writes (`parent-closes-after-fork` in `tests/unit/fork_scenarios.py`, found by review 07).
- Why: pre-fork servers (gunicorn, Celery prefork) fork while background threads may be using the cache, and a 60 s stall in a fresh worker is hard to diagnose.
- See D-027 for what a thread behind that hook does while it waits.
- Tests: `test_processes.py` (`test_fork_while_another_thread_holds_a_write_transaction` is a strict xfail on diskcache; `test_fork_while_another_thread_writes_the_cache` is oxide-only). The barrier must cover every `Cache` in the process, not just the one the fork goes through (upstream #325): `test_fork_while_another_thread_writes_through_a_second_cache` (oxide and community) hammers writes through a second `Cache` on the same directory while the main thread forks with the first. On diskcache a child's write timed out in 13/25 runs on 3.14 and 15/25 on 3.14t (two forks per run); one child on 3.14 hung until it was killed instead.

## D-013 A missing value file is a miss, and the row is removed

- Kind: test-visible
- diskcache: if a large value's file disappears from disk, `get` returns the default, but the row stays: `in` is True and `len` counts it, until an explicit `delete` or `check()`. The diskcache-community fork deliberately keeps this behavior.
- oxide: `get` returns the default, and the orphaned row is removed. The same holds for a row whose stored file name isn't one oxide writes (`aa/<32 hex>.val`, D-042), which is never turned into a path, so a damaged row can't reach outside the cache directory. Because reads must never block on or wait for the write lock, the removal is opportunistic: `get` tries it without waiting, never raises if the lock is busy, and leaves any row it couldn't remove to the next write's cleanup.
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
- oxide: a cache directory that diskcache-oxide creates, and any subdirectories it creates for value files (D-042), are 0700, including one a sweep removed and a later write re-created. Intermediate parents follow the umask, like `os.makedirs`. An existing directory's permissions are never changed.
- Why: a cache directory is a security boundary. Anyone who can write to it can poison the cache, and its contents (e.g. keys containing IP addresses) can be private.
- Tests: `test_basic.py`

## D-016 A key that's present the whole time never reads as missing while its value is overwritten

- Kind: test-visible
- diskcache: while one process overwrites a spilled (file-backed) value and other processes `get` it, an occasional read returns the default, though the key is never deleted. No torn values, `None` or exceptions were seen, and afterwards the key, `len` and the value-file count are all correct. Measured with `test_processes.py`'s Test A: the default variant (4 reader processes, 500 overwrites of 100-400 KB values) failed in 5/20 runs on 3.14 and 3/20 on 3.14t (a lighter 3-reader, 150-overwrite version failed in 1/75 and 5/75); the heavy variant (6 readers, 3000 overwrites) failed in 10/10 runs on both. The standalone repro sees about 40 misses in 325k reads, every run. The diskcache-community fork has the same bug. Found by the Phase 0.5 tests (upstream #294, and #258 for threads).
- oxide: a read of a spilled value that finds its file missing reads the row again and retries if the file reference or `store_seq` changed, so it always returns a whole value, old or new. It's a miss only when the row still points at the same missing file (D-013), and the D-013 cleanup is a compare-and-delete, so a reader can never delete a live row a writer just replaced.
- Why: `get` with a default is how callers decide to recompute a value. Spurious misses on hot, large values cause needless recomputes, and a reader that "cleans up" after one would lose the live value.
- Tests: `test_processes.py` (`test_reading_a_spilled_value_while_it_is_overwritten_never_misses` and its `slow` heavy variant are oxide-only, because the failure on diskcache and the fork is random). The locked fallback is driven deterministically by the core's `ops::tests` (`a_reader_that_keeps_losing_to_overwrites_*`, `a_value_file_missing_or_short_under_the_lock_is_a_miss_and_its_row_goes`, `the_fallback_waits_for_the_write_lock_within_the_whole_calls_timeout`), through test-only hooks that overwrite the value before each optimistic read and before the fallback.

## D-017 Connections and fds are bounded by the pool

- Kind: test-visible
- diskcache: one shared `Cache` opens a SQLite connection for every thread that uses it, and each holds the database and its WAL open. With 64 threads held after one `get` and one `set` each, 131 fds are open inside the cache directory (2 × (threads + 1) + 1 for the shared `-shm`), on 3.14 and 3.14t. A server with many threads can run into the fd limit (upstream #295, #336).
- oxide: connections come from a bounded pool, so the fd count inside the cache directory is at most 2 × pool size + 1, however many threads are alive (`OXIDE_FD_BUDGET` in `backends.py`), apart from the pending descriptors described below. The pool is per database file and shared by every handle on it. Its size is the largest `max_connections` any live handle asked for (default 8; 04-design.md section 7). A nested operation inside a held transaction may open over the cap, and a forked child deliberately leaks any connection it inherited (idle ones are closed before the fork, so only those the forking thread was using), and each store's pin (an fd on the database file, a connection that never runs a statement), which it forgets rather than closes, so fd tests that combine forks must allow for that. A FanoutCache has one pool per shard, and its budget is in D-037. One more thing to know when counting: SQLite's unix VFS defers closing a descriptor on the database file while any connection in the process holds a lock on that file, which every pooled connection does for its whole life in WAL mode, so a connection or pin closed beside a live pool shows as one extra bare fd on the database file until the pool empties (the last `close()`, or the connections closed before a fork). What closes one beside a live pool: a connection opened over the cap by a nested operation; an open that raced another thread's first open of the directory and lost, if the winner registered while the loser was opening its pin (its pin), or if the winner's pool already held its idle cap (its connection: a losing open otherwise hands its connection to the winner's pool and opens no pin, plan 20); and a closed handle's rejoin that found its store retired and lost the revive to another thread (its pin). SQLite reuses a pending descriptor for the next open on the file in the same mode (read-only for a pin, read-write for a connection), so one-off events leave at most one per mode, but closes that come together before any new open pile up: 16 threads making a process's first open of one directory at once left 8 to 16 pending on the database file (18 to 25 before plan 20) with every handle open. All of them close when the pool empties: the last `close()`, or the connections closed before a fork. A database file replaced in the middle of an open fails that open with `OSError` ("replaced while it was being opened") rather than serving the old file's rows. A closed handle used again (it rejoins its store) opens nothing while the store is live; it used to open a pin and close it, which left that one fd behind.
- Why: the number of open fds should depend on configuration, not on how many threads happened to touch the cache.
- Tests: `test_threads.py` (`test_peak_fds_bounded_by_the_pool` is a strict xfail on diskcache; `test_peak_fds_within_the_backend_budget` pins each adapter's `fd_budget`; `test_a_closed_handle_used_again_beside_a_live_one_adds_no_fd` and `test_racing_first_opens_all_read_the_seed_and_close_leaves_no_fd` are oxide-only), the core's `store::open_race` tests (racing first opens, the idle cap, a replaced file), and for a FanoutCache `tests/unit/test_fanout.py` (`test_a_closed_handle_used_again_beside_a_live_one_adds_no_fd_on_any_shard`, `test_reopening_under_a_running_iteration_stays_within_the_fd_budget`)

## D-018 close() closes every connection

- Kind: test-visible
- diskcache: `close()` closes only the calling thread's connection. After 64 threads have used a shared `Cache` and exited, `close()` leaves 130 fds open inside the cache directory until a gc pass finalizes the connections, and each one then reports `ResourceWarning: unclosed database` (64 warnings, on 3.14 and 3.14t).
- oxide: connections belong to a per-file `Store` shared by every handle on that file. When the last open handle calls `close()`, every connection is closed, whichever threads used it. No fd inside the cache directory stays open (apart from the one fd the `Store` keeps on the database file, its pin, while any handle is alive; the tests count after the last close). In a forked child, an inherited store's pin is forgotten, never closed (D-017), so the child's last `close()` leaves that one fd per database open, and nothing warns at finalization. The exception is a `close()` that gave up behind a stuck fork: those connections close when the fork ends (D-027).
- Why: after `close()` the directory must be safe to delete or move, and leaked connections hide in tests behind `ResourceWarning` noise.
- Tests: `test_threads.py` (`test_close_releases_every_fd` and `test_threads_then_close_emit_no_resource_warning` are strict xfails on diskcache), and for every shard of a FanoutCache, `test_fanout.py` (`test_close_releases_every_fd_on_every_shard`, a strict xfail on diskcache) and the `fanout-close-after-fork` scenario in `tests/unit/fork_scenarios.py`

## D-019 Opening with unchanged settings never writes

- Kind: test-visible
- diskcache: opening an existing cache with the same settings commits a write transaction (from outside it looks like the Settings rows being rewritten with the same values), so a peer connection's `PRAGMA data_version` goes from 2 to 3 on open, even when every operation afterwards is a read. The main database file's bytes don't change, so only `data_version` catches it.
- oxide: opening with unchanged settings, then `get`, `in`, `len`, `iterkeys` and `volume`, never write: a peer's `data_version` is unchanged, and so are the main database file's hash and mtime.
- Why: a read-mostly service (or a read-only replica of the directory) shouldn't take the write lock or dirty the WAL just by opening its cache.
- Tests: `test_basic.py` (`test_open_and_read_never_write` is a strict xfail on diskcache)

## D-020 Forking inside your own transaction fails fast

- Kind: note (pinned by `tests/unit/test_transact.py` through `transact()`, and by the `d020` scenario in `tests/unit/fork_scenarios.py`)
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
- oxide: serialization is chosen with `serializer=` and `pickle=` instead of a `Disk` class, and there's no plan to support custom `Disk` subclasses; a type the codec doesn't know is registered per type instead (D-041). `eviction_policy` is always least-recently-stored until the other policies arrive in Phase 6a. Only `directory` and `timeout` can be passed positionally.
- Why: a pluggable Python `Disk` would put Python code on every read and write in the Rust hot path.

## D-023 sqlite_cache_size is a per-process memory budget, 1 MiB by default

- Kind: note
- diskcache: `sqlite_cache_size` (in pages, default 8192, so 32 MiB per connection) is a setting stored in the cache's `Settings` table, and it comes with a 64 MiB `sqlite_mmap_size`. Every thread opens its own connection, so SQLite's memory grows with the thread count.
- oxide: `Cache(..., sqlite_cache_size=pages)` takes the same unit (4 KiB pages) but is a per-process option, not stored in the database. The default is 256 pages (1 MiB). Within one process, all handles on a directory share a pool of at most `max_connections` (default 8) connections, so SQLite's page caches for that directory stay at about `max_connections × sqlite_cache_size` pages (8 MiB by default). A FanoutCache's budget is for the whole root: `max_connections` defaults to two per shard (D-037), about 16 MiB at 8 shards. A held transaction or a nested operation can open one connection beyond the cap. The largest value any handle asks for applies, as with `max_connections`. There's no `sqlite_mmap_size`. `tests/unit/test_memory.py` and `bench/memory.py` hold and report the bound. They run on a memory-instrumented build, since the shipped one turns SQLite's memory statistics off for speed.
- Why: bounded, predictable memory in long-running web workers. The default was the smallest size that kept hot and skewed reads, writes and the threaded mixes unchanged. It costs about 5% on the worst case (uniform random reads over a database much larger than the cache, the `get_random_wide` benchmark), where it's still about 0.8× diskcache's latency. Raise it for large, uniformly read caches: 2000 pages measured about 17% faster there.

## D-024 The WAL file can be trimmed and checkpointed (wal_size_limit, checkpoint())

- Kind: note
- diskcache: leaves SQLite's WAL to its defaults. With readers in other processes, a burst of writes (a fill, a `clear()`) can't wrap the WAL, so the file grows to fit the burst and stays that size: WALs of hundreds of MiB to over 1 GiB next to a database of a few MiB.
- oxide:
  - **Always on, and free:** `Cache(..., wal_size_limit=bytes)` (default 64 MiB, at least 1 MiB; per process, not stored; the largest value any handle asks for wins, as with `sqlite_cache_size` in D-023) sets `journal_size_limit` to half the limit, so a WAL that resets normally shrinks back. It also scales `wal_autocheckpoint` from the limit. These are connection settings: they never pause anything. At normal load they keep the WAL at a few MiB (the reasonable-load tests peaked at 1.4-4 MiB).
  - **On demand:** `Cache.checkpoint()` checkpoints and truncates the WAL to zero, retrying until the cache's `timeout`. It returns `True` if the WAL was reset, `False` if readers or writers in other processes, or a pending fork (D-027), kept it busy, and never raises for busy. A real error (a full disk, an I/O error) raises at once instead of being retried, and in a child forked inside a transaction it raises like any write (D-020). The timeout bounds the waiting, not a round already under way: on a saturated disk, copying a large backlog can take longer, and the truncation's first try always gets its own 2 ms busy window. Its later tries (see the automatic mode below) stop waiting at the timeout. Inside the calling thread's own held transaction it returns `False` without trying. Call it when a short pause suits the app: at shutdown, in a maintenance job, or after a large `clear()`.
  - **At close:** `close(checkpoint=True)` (the default) runs it first on the process's last handle, if the WAL is past its trim size (half the limit), waiting at most 1 second for other processes, and not again for 1 second after an attempt that didn't succeed. A small WAL, the usual case, costs one `stat`. Close never raises because of it, and closing twice is harmless. `close(checkpoint=False)` skips it. SQLite also checkpoints and removes the WAL by itself when the last connection to the database closes in any process.
  - **Opt-in, automatic:** `wal_checkpoint="auto"` runs that checkpoint after writes, at most every 250 ms or 64 writes, once the WAL is over the limit. It does `PASSIVE`, then `TRUNCATE`, trying `TRUNCATE` up to 8 times while it reports busy, each try in a fresh 2 ms busy window, with the interpreter released and a 1 s backoff if every try was busy, and never fails the write. The tries matter because `PASSIVE`, right after a commit, often copies nothing past a reader on an older snapshot: the first `TRUNCATE` then spends its window on the copy and loses to any reader in flight, while a later one has its whole window for the readers. Under sustained writes beside readers in other processes, one try failed most forced checkpoints, and the WAL reached many times its limit (plan 09, step 2b). No later try starts past the write's `timeout` or once a fork is pending. So a fork that arrives mid-round waits for the try under way, and one that arrives sooner waits for the rest of `PASSIVE` and the first try, which always runs and is where the copying is. The copying isn't bounded by any of this: a try's copy and syncs can run past the `timeout` (855 ms measured beside other writers on a saturated disk), and the 1.4 s seen in the soak is an observation, not a bound. A write with a `timeout` under about 35 ms can time out behind a round. Under saturated disks (the soak), the triggering write could still pause for seconds. That's why it isn't the default (Michael, 2026-10-28).
  - **`wal_size_limit` is a target, not a bound.** Sustained writes from several processes near flat out can take the WAL to tens of times the limit: 6 GiB on 3.15t, where about 12% of forced checkpoints succeeded, and 21 GiB on 3.14 in 10 minutes, where 1% did (18 of 1,687). It's much smaller below flat out, for example 250 MiB at about 2,600 writes a second across 3 writers in `auto`. In `manual` mode with busy readers in other processes, the WAL grows until something checkpoints, so an app with that shape needs `auto` or periodic `checkpoint()`, and a script should call `checkpoint()` or `close()` after a large fill.
  - Found by the Phase 2.5 soak; `tests/unit/test_wal.py` holds it, and `python -m bench.wal_burst` measures it.
- Why: a cache's disk use should be predictable, and no write should pay for housekeeping unless the app asked for it. Between checkpoints, the WAL is at most as large as the largest burst since the last one; a later burst reuses the file instead of adding to it.

## D-025 A key iterator used by two threads at once raises ValueError at once

- Kind: note (pinned by a unit test, not a conformance test)
- diskcache: `iter(cache)` and `iterkeys()` return generators. A thread that calls `next()` while another thread is inside the same generator (for example, in SQLite with the GIL released) gets `ValueError: generator already executing`.
- oxide: they return a native `KeyIterator`. A `next()` while another thread is fetching from the same iterator raises `ValueError("iterator already executing")` at once, never waiting for the other thread. The exception type matches. The differences: the message, and the iterator isn't a generator, so it has no `send()`, `throw()` or `close()`.
- Why: waiting would block a thread with the interpreter attached behind one that may be waiting, detached, for a pending fork, which hung the whole process (review 07). Raising is what a shared generator already does.
- Tests: `tests/unit/test_concurrency.py` (fork scenario `shared-iterator-while-a-fork-waits`)

## D-026 pop decodes before it deletes

- Kind: note (pinned by `tests/unit/test_pop.py`; the racing tests in `test_threads.py` and `test_processes.py` run on every backend)
- diskcache: `pop` reads and deletes the row inside one write transaction, then deserializes it, so the row is gone before anything runs on the value.
- oxide: `pop` reads and decodes the value with no write lock, as `get` does, then deletes exactly that stored version in its own transaction. Each value still reaches exactly one caller. If another writer replaced or removed that version first, `pop` reads again, and after 8 tries takes a locked removal that decodes afterwards. What a caller can see:
  - A pickle hook (`__setstate__`) sees the key still present.
  - A hook that raises `Timeout`, `MemoryError`, `RecursionError`, `OSError` or a `BaseException` leaves the row in place. So does a foreign value, whatever `on_decode_error` says. A value no build can read is still deleted, and under `'raise'` the error comes after the delete. The exception is the locked removal after 8 tries: it removes before it decodes, so there a foreign value or any hook error costs the value.
  - `pop` of a missing key doesn't take the write lock, so it no longer times out behind a writer.
  - A hook that pops its own key now recurses, since the key is still there each time: `RecursionError` at the default recursion limit. Each level holds a connection, so a low fd limit gets there first, as an `OSError` (depth 125 at `ulimit -n 256`). A spilled value always reaches `RecursionError`, even at `ulimit -n 64`, since no connection is held during its decode. Nothing is deleted on the way out.
  - A hook that overwrites its own key loses the race on every try, so it runs up to 9 times and ends in the locked removal.
  - A row whose value file is missing can stay after `pop` when the write lock is busy, as with `get` (D-013). It's a miss either way, and a later `pop` or write removes it.
  - `timeout` covers the whole call, including the time spent in hooks and on lost races, not each step. A race lost after it has run out raises `Timeout` rather than going on to the locked removal: this `pop` deletes nothing, and whatever the other writer left stays.
  - With `durable=True` (D-028), a failed delete or read is the default and the row stays, and a hook's `Timeout`, `OSError` or `RecursionError` is the default with the row kept.
- Why: a newer peer's value in an unknown format must survive a `pop` during a deploy, and a transient error in a hook shouldn't cost the value.

## D-027 A pending fork counts against `timeout`

- Kind: note (pinned by `tests/unit/test_fork_timeout.py`)
- diskcache: has no fork barrier, so its calls never wait for a fork.
- oxide: a cache call waiting behind a pending fork (D-012) returns within its `timeout`, plus a poll and the wait for the interpreter, raising `Timeout`. Its `timeout` is one budget, shared with the write lock, a connection slot and SQLite's lock, and `Cache(...)` raises within its one `timeout` too. What doesn't raise:
  - `checkpoint()` returns `False`, and doesn't start the 1 s backoff a busy WAL does.
  - `close()` and `del` never raise, and return within about 2 s and 1 s behind a stuck fork, whatever `timeout` is. A `close()` that gave up leaves its connections for the end of the fork, and the store looks like it has one more handle until then.
  - Opportunistic cleanup (a lost value file's row, an unreadable value on a read) skips instead of waiting, and a later read retries.
- A thread holding a transaction, or running in a pickle hook on an inline value, holds a gate level and passes the fork, so it never times out at the gate. Everyone else waits. So while a fork is stuck, every thread with no level gets `Timeout` on every `Cache` in the process. Other processes aren't affected.
- `os.fork()` itself can still wait forever, and a second `os.fork()` queues behind it. CPython ignores exceptions from before-fork hooks, so a fork can't be refused, and proceeding without quiescence would hand the child half a transaction: the D-012 stall at best, a WAL deleted under the child at worst. A stuck fork is a visible problem, and a corrupt child isn't.
- Every wait on a held transaction (`transact()`) is bounded by that transaction's `timeout`. Ending one from another thread while its owner is forking or parked, and finalizing an inherited one in a child with a stuck fork of its own, give up at the deadline: the end is parked, and the transaction is rolled back at the end of the fork (a parked commit raises `Timeout`, a parked rollback returns normally). A commit from another thread waiting for the owner's operation gives up at its deadline, raises `Timeout`, and is left to that operation, which rolls back when it hands the connection back. Between a park and the drain the transaction is still the owner's, and the owner's writes land in it and roll back.
- A fork landing in the instant between a deferred release being counted and its flag being set gives that child a count with no flag: nothing drains it there unless a later deferral does, so that handle's connections stay open in that child (D-018 is weaker in that child only). A parked end can likewise leave a child with a `DEFERRED_ENDS` entry but no flag; that only leaks memory, since the hold is poisoned. The child's connections are forgotten rather than closed anyway, nothing is corrupted, and the parent is unaffected.
- Importing `diskcache_oxide` also imports `logging` and `concurrent.futures.thread` (about 1.6 ms), so its before-fork hook runs before theirs and their locks are only taken once the fork has quiesced.
- Why: `timeout=` should mean what it says, and a thread that depends on another thread's cache call while a fork is pending used to wedge the process's whole cache layer, with no error.
- Tests: `tests/unit/test_fork_timeout.py`, `crates/diskcache-oxide-core/src/store/fork_timeout.rs`, and the gate's models.

## D-028 durable=True answers an outage on a single-key call as a miss

- Kind: note (pinned by `tests/unit/test_durable.py`; the option doesn't exist on diskcache, and it's off by default, so nothing changes unless it's asked for)
- diskcache: `Cache` has no such option, and its calls raise `Timeout`, an `OSError` or a `sqlite3` error. `FanoutCache` answers `Timeout` alone as a miss or False, and raises the rest.
- oxide: with `durable=True`, an operational failure on a single-key call doesn't raise. `get`, `pop` and `in` give the default or False, `cache[key]` raises `KeyError`, `set`, `add` and `touch` give False, `cache[key] = v` drops the write, `incr` and `decr` give `default ± delta` as `NullCache` does (`default=None` raises `KeyError`), `delete` gives False and `del cache[key]` raises `KeyError`. `on_decode_error` defaults to `'miss'`, and an explicit value wins.
- What's operational is one exhaustive match, `Error::is_operational()`: `Timeout`, `OSError`s, SQLite errors including a damaged database, and a child forked inside a transaction (D-020). A failure is only swallowed where a store call returned it, so a `Timeout` from the caller's own `clock=`, or from a pickle hook during a write, still raises.
- Still raised: bugs (`TypeError`, `ValueError`, `UnsafeValueError`, a non-numeric `incr`, an overflow, misuse of a transaction), `MemoryError` including `SQLITE_NOMEM` (which was an `OSError` before this, on any handle) and `KeyboardInterrupt`, the constructor and starting a transaction (`transact()`), admin calls (`clear`, `expire`, `cull`, `checkpoint`, `len`, `volume`, iteration), and anything on a store while this thread holds a transaction on it.
- A pickle hook's `OSError` (`Timeout` included) or `RecursionError` on a read is a miss with the row kept, reported once per stored version, unless `on_decode_error='raise'` was passed. `MemoryError`, `BaseException` and `UnsafeValueError` still raise.
- A durable `pop` whose delete fails returns the default, never the decoded value, which could otherwise be delivered twice.
- It can drop writes, and that's the point of the name's warning: in database vocabulary "durable" means "survives a crash", and here it means the cache keeps the app running. A dropped `set`, `delete`, `pop` or version-bump `incr` serves stale data until the entry expires, and forever with no expiry. A fake `incr` is a duplicate if used as an id. `add` is False under durable but True on `NullCache`, so a claim needs a handle that isn't durable. It doesn't bound time either: a call still takes up to `timeout`, which must still exceed work in flight plus a fork plus the interpreter hand-off (D-027).
- Reporting: three buckets on each handle, busy (`Timeout`, warning), broken (everything else, error) and lost deletes (any failed `delete`, error), so a timeout storm can't hide a damaged database. Each logs its first failure at once and then at most once per 60 s, with per-operation counts, timed by the failing call's own clock reading, and `close()` logs what's left. A hook outage on a read is reported once per stored version and counted in busy or broken, with no line of its own. `_durable_stats()` keeps the totals.
- Why: Talk Python's callers all fall back to a source of truth, so a cache failure should cost a miss and not a failed request.
- Tests: `tests/unit/test_durable.py`, `tests/unit/durable_scenarios.py`, and `error.rs` for the classification.

## D-029 `transact()` hands back the cache, and every handle on the directory shares it

- Kind: note (pinned by `tests/unit/test_transact.py`; the conformance tests use the cache itself, not the `as` target)
- diskcache: `with cache.transact() as tx:` binds `tx` to `None`. A second `Cache` on the same directory has its own connection, so its `transact()` inside the first one's block waits for the write lock until it times out.
- oxide: `tx` is the cache itself. On the thread that entered the block, every call on that directory runs in the transaction, through any handle, and a `transact()` on a second handle inside it joins the outer one (its exit commits nothing) instead of waiting. Other threads' calls don't join it. The context manager (`Transaction`) is single use: entering it twice, or while another thread is entering it, raises `RuntimeError`.
- Why: a superset of diskcache's `None`; a proxy object would be a second API with no new guarantee, since only the owner thread's calls join the transaction anyway.

## D-030 A transaction on a second, busy cache fails at once with `TransactionBusy`, and `retry=True` retries only `Timeout`

- Kind: note (pinned by `tests/unit/test_transact.py` and `tests/unit/test_concurrency.py`)
- diskcache: inside one cache's `transact()`, another cache's `transact()` waits for its write lock until it times out, and `retry=True` loops on any `sqlite3.OperationalError`.
- oxide: when another thread holds the second cache's write lock, the inner `transact()` raises `TransactionBusy` (a `Timeout` subclass) at once: that thread could be waiting on a lock this one holds. A free lock is taken. `retry=True` retries a plain `Timeout` and never `TransactionBusy`. Each attempt waits up to `timeout` with the interpreter released, and signals (`KeyboardInterrupt`) are checked only between attempts.
- Why: waiting there risks a deadlock that no timeout setting fixes, and retrying it would turn the deadlock into a livelock.

## D-031 A transaction may end on another thread, within its `timeout`

- Kind: note (pinned by `tests/unit/test_transact.py`)
- diskcache: a transaction "may not be shared between threads".
- oxide: any thread may call the `Transaction`'s `__exit__`, committing (no exception) or rolling back the owner's transaction. While the owner is inside one of its operations, a rollback is left to that operation, which rolls back when it returns, and a commit waits for it until `timeout`; a commit that can't run by then raises `Timeout` and rolls back. The owner's own exit afterwards raises `TransactionAborted` (a `RuntimeError`) unless the transaction committed, so a `with` block never exits cleanly when its writes are gone. An exit with no exception that comes while another thread's end is still running waits for its outcome, up to the cache's `timeout`, and past that raises `Timeout` ("whether it committed is unknown"); an exit with an exception passing returns at once. Writes the owner makes after another thread ended it run outside the transaction.
- Why: the transaction's locks must not outlive a thread that's stuck, and an end from elsewhere must never be mistaken for the owner's commit.

## D-032 FanoutCache records its shard count, and a different `shards=` raises

- Kind: note (pinned by `tests/unit/test_fanout.py` and `tests/unit/test_shard_identity.py`)
- diskcache: `FanoutCache(directory, shards=8)` records nothing. Opening the same directory with another `shards=` silently sends keys to other shards: existing entries read as missing, a second `add` of the same key can win in each process, and `incr` totals split, across processes that disagree.
- oxide: the root holds a manifest (`diskcache-oxide-fanout`) with the shard count and the hash's identity, created atomically (a temporary file and a hard link, so of two racing creators exactly one wins and the other reads its file). `shards=None` (the default) reads the stored count, or creates 8. An explicit `shards=` that differs raises `LayoutMismatchError` (a `ValueError`, and a `FormatMismatchError`) naming both numbers, so changing the count means a new directory. Each shard's database also records its index, the count and the hash, and a renamed, copied or mismatched shard is refused. Creating the manifest needs hard links, so a filesystem without them (FAT/exFAT, SMB, some FUSE) raises `OSError` saying so. A leftover `diskcache-oxide-fanout.tmp-*` file from a crash is ignored and safe to delete.
- Why: the shard count is part of where every key lives, so a mismatch has to be an error, not a quiet loss of data and atomicity.
- Tests: `test_the_stored_count_wins_and_another_is_refused`, `test_processes_racing_to_create_one_root_agree_on_one_manifest`, and the shard-identity tests

## D-033 Shards are chosen by XXH3, and iteration goes shard by shard

- Kind: note (the conformance tests compare iteration as sets)
- diskcache: a key's shard is `adler32` of its stored form, modulo the shard count. `iter(cache)` chains the shards' iterators, `reversed()` chains the shards backwards, and there is no `iterkeys()` on `FanoutCache` (it's on `Cache`).
- oxide: a key's shard is XXH3-64 (seed 0) of its key-type byte and its UTF-8 bytes, mapped to the shard by a multiply-high, and `FanoutCache.shard_of(key)` says which. The hash is part of the on-disk format, pinned by golden vectors, so every process and every release agrees. `iter(cache)` yields every key once, shard 0 to N-1, each shard in its insertion order: so not in the cache's insertion order, and not in stock's order, since the shards hold different keys. `iterkeys(prefix=, reverse=)` merges the shards into one sorted order, the order a plain `Cache` gives, holding no connection between yields. Neither is a snapshot: concurrent writes may or may not be seen, never twice. There's no `reversed()` (yet).
- Why: adler32 spreads short and similar keys poorly, and a hash that's part of the format must be fixed by its definition, not by an implementation that could change. The sorted merge gives FanoutCache the ordered listing `Cache` has.
- Tests: `tests/unit/test_fanout.py` (the golden table, `test_iter_yields_every_key_once_shard_by_shard_each_in_insertion_order`, `test_iterkeys_merges_every_shard_into_the_order_sorted_and_a_plain_cache_give`)

## D-034 A FanoutCache's single-key calls raise `Timeout`, unless `durable=True`

- Kind: test-visible
- diskcache: `FanoutCache` swallows `Timeout` on single-key calls: `set`, `add`, `touch` and `delete` give False, `get` and `pop` give the default, and `incr` and `decr` give None. `get` also swallows any `sqlite3.OperationalError`. `cache[key]`, `cache[key] = v` and `del cache[key]` instead retry forever. Its default `timeout` is 0.010 s, so a briefly busy shard drops writes silently.
- oxide: single-key calls behave exactly as on `Cache`, on the key's shard: a busy shard raises `Timeout` after `timeout` (60 s by default), and so do `[]`, `[]=` and `del`. `durable=True` gives the miss answers instead, logged and counted as on `Cache` (D-028), with one report for the whole FanoutCache, named by its root directory.
- Why: a silently dropped write and a hang are both worse than an error the caller chose how to handle; `durable=True` is that choice, with the logging stock lacks.
- Tests: `tests/conformance/test_fanout.py` (`test_a_write_to_a_locked_shard_raises_timeout` is a strict xfail on diskcache; `test_a_durable_fanout_cache_answers_a_locked_shard_as_a_miss` is oxide-only)

## D-035 Fanned-out maintenance shares one `timeout` and reports partial progress

- Kind: note (pinned by `tests/unit/test_fanout.py`)
- diskcache: `clear`, `expire`, `cull` and `evict` call each shard in turn and retry it forever on `Timeout`, so a shard that stays locked hangs the call, and nothing says which shards were done.
- oxide: `clear`, `expire`, `cull` and `checkpoint` take `timeout=` (default: the handle's) for the whole call, not per shard, as do opening, `len()`, `volume()` and `close()`. A shard reached after the deadline still gets one try with no wait, so idle shards are always done. A failing shard doesn't stop the others; afterwards the call raises the first failure's own type (so `except Timeout` works) with `.failed_shards` (a tuple of indexes), `.removed` (the total removed from every shard, except on `checkpoint()`) and a note naming them. A removal takes every shard's snapshot first, so a write made during the call survives on every shard, as on `Cache` (D-011). `checkpoint()` is True only if every shard's WAL was reset. `durable=True` doesn't swallow any of these (D-028).
- Why: a maintenance job must finish in bounded time, and when it can't, it must say what it did.
- Tests: `test_a_locked_shard_fails_alone_and_the_error_says_how_far_the_call_got`, `test_one_timeout_bounds_the_whole_call_not_one_per_shard`, `test_at_a_zero_timeout_every_idle_shard_is_still_emptied`, `test_checkpoint_is_true_only_when_every_shard_resets`, and the `fanout-fork-mid-clear` scenario in `tests/unit/fork_scenarios.py`

## D-036 `size_limit` is split evenly with a 1 MiB minimum per shard, and reads back as the total

- Kind: note (pinned by `tests/unit/test_fanout.py`)
- diskcache: divides `size_limit` by the shard count as a float, with no minimum (1 GiB / 8 = 134217728.0), and `cache.size_limit` reads shard 0's share.
- oxide: each shard gets `floor(size_limit / shards)` and culls on its own. Each needs at least 1 MiB (D-005), so the total must be at least `shards` MiB, or `ValueError`. With none given, a new root gets 1 GiB in all, split the same way. `cache.size_limit` is the sum of the shards' stored limits.
- Why: an integer budget per shard is what's stored, and the D-005 floor applies to every shard, since a shard is a whole cache.
- Tests: `test_the_default_size_limit_is_a_gibibyte_in_all`, `test_an_explicit_size_limit_is_split_floored_and_kept_on_reopen`, `test_a_size_limit_under_a_mebibyte_per_shard_is_refused`

## D-037 `max_connections` is a total across the shards, so fds are bounded too

- Kind: test-visible
- diskcache: every thread opens its own connection to each shard it uses (D-017, once per shard). With 64 threads that each did one `get` and one `set` on an 8-shard FanoutCache, 264 fds are open inside the root, on 3.14 and 3.14t, and `close()` leaves 256 of them open (D-018).
- oxide: `max_connections` is the most connections the process opens for the whole FanoutCache, split across the shards (`max_connections // shards` each, the first `max_connections % shards` getting one more). It must be at least `shards` (`ValueError`), and the default is two per shard, never fewer than 8 in all: 16 at 8 shards, so SQLite's page caches (D-023) are about `16 × sqlite_cache_size`, twice a default `Cache`'s. One per shard read 16-31% below a plain `Cache` with threads sharing a handle (plan 12, M2). Each connection holds its shard's database and WAL, and each shard adds its `-shm` and its pin, so the fds inside the root stay within `2 × max_connections + 2 × shards`, however many threads there are: 48 at the defaults, measured at 32-34 with 64 threads on 3.14t, and 64 (exactly the budget) at `max_connections=24` on 3.14t. `close()` leaves none. Opening takes about 4 fds per shard, so 256 shards need over 1,024, Linux's default `ulimit -n`. Too few raises `OSError` with `errno` `EMFILE` and a message giving the shard count, the fds needed, the limit and how many are already open, with SQLite's error as the cause; a failed open leaves no fd open. A busy shard gets only its share of connections: raise `max_connections` if a measurement says a hot shard needs more.
- Why: the fd and memory cost should follow configuration, and sharding shouldn't multiply it by the thread count.
- Tests: `tests/conformance/test_fanout.py` (`test_peak_fds_bounded_by_the_shard_budget` is a strict xfail on diskcache), `tests/unit/test_fanout.py` (`test_many_threads_stay_within_the_fd_budget_and_close_leaves_none`, `test_max_connections_must_cover_every_shard`, `test_too_few_file_descriptors_for_every_shard_names_the_numbers`)

## D-038 A plain `Cache` and a FanoutCache refuse each other's directories

- Kind: note (pinned by `tests/unit/test_fanout.py` and `tests/unit/test_shard_identity.py`)
- diskcache: a `Cache` opened on a FanoutCache's root creates its own database there, and one opened on a shard directory (`000`) uses that shard as an ordinary cache, writing keys that the FanoutCache may route elsewhere.
- oxide: `Cache` on a FanoutCache root, or on one of its shards, raises `LayoutMismatchError` (still a `ValueError`), and `FanoutCache` on a plain `Cache`'s directory does too. Each checks for the other's marker before writing its own, so the refused open creates nothing: `Cache(fanout_root)` leaves no database behind. That holds while the FanoutCache is open in the same process, too.
- Why: a typo shouldn't leave a directory that neither kind can open again, or a shard written behind the FanoutCache's back.
- Tests: `test_a_plain_cache_on_the_root_raises_and_creates_no_database`, `test_a_fanout_cache_on_a_plain_cache_raises_and_writes_no_manifest`, `test_a_plain_cache_on_a_live_shard_raises_and_leaves_the_count_alone`, `test_a_plain_cache_racing_fanout_caches_for_one_root_never_both_open`

## D-039 FanoutCache's constructor and the stock features it doesn't have (yet)

- Kind: note
- diskcache: `FanoutCache(directory=None, shards=8, timeout=0.010, disk=Disk, **settings)`, so `FanoutCache(path, 4)` asks for 4 shards. It also has `transact()` across every shard, `cache()`, `deque()` and `index()` sub-collections, `stats()`, `evict()` and tag indexes, `check()`, `reset()`, `read()`, `reversed()`, the `retry=`, `read=` and `tag=` arguments, settings read as attributes from shard 0, and pickling.
- oxide: `FanoutCache(directory, timeout=None, *, shards=None, ...)`, with the same options as `Cache` plus `shards`. `shards` is keyword-only, so stock's `FanoutCache(path, 4)` would be a 4-second timeout here: pass `shards=4`. None of the features above exist yet except `transact`, which here is `transact(key=...)`, scoped to one shard (D-040); the rest come with Phase 6a and 6d. `directory` is required (D-021), and there's no `disk=` (D-022).
- Why: the positional `timeout` matches `Cache`'s signature, and each missing feature needs its own design across shards rather than a copy of stock's.

## D-040 FanoutCache.transact(key=...) covers one shard and refuses other shards' keys, reads included

- Kind: test-visible
- diskcache: `FanoutCache.transact(retry=True)` takes every shard's transaction in shard order and yields `None`, so it is atomic across all keys and every other thread's and process's writes to any shard wait for it. Its `retry` defaults to True, and `retry=False` fails an `assert` (5.6.3), so the usual call is a bare `with cache.transact():`.
- oxide: `transact(*, key, retry=False)` is `Cache.transact()` (D-029 to D-031) on the shard `key` lives in, and `tx` is the FanoutCache. `key` is keyword-only and required, so a switched-off cache's `NullCache.transact(key=...)` takes the same spelling, and `transact('k')` is a `TypeError` rather than a `retry` of 'k'. A ported `with cache.transact():` raises `TypeError` too, since `key` is required: name a key on the shard the block uses. Only that shard is locked: writes to the other shards, from other threads and processes, carry on. On the thread that entered it, a call for a key on any other shard of the root raises `ValueError` naming both shards, reads included and through any handle on the root (the stores are shared, as in D-029), and so does entering a `transact` for a key on another shard; a nested `transact` for a key on the same shard joins the outer one. `shard_of(key)` says which keys can share one. Calls over every shard are refused the same way, with `ValueError` naming the call and the transaction's shard: `len()`, `volume()`, `iter()`, `iterkeys()` (and `next()` on an iterator made before the block), `clear()`, `expire()`, `cull()` and `checkpoint()`. Inside a one-shard transaction, only that shard's keyed calls reach the cache. Other threads, another root's transaction and a plain `Cache`'s transaction refuse nothing. `close()` isn't refused: it never raises, and `__exit__`, garbage collection and `Caches.close_all()` rely on that. Under `durable=True` a failed begin, and any failure inside the block, raises (D-028). One gap: while a fork is pending, the owner of a transaction parked behind that fork (D-031) isn't refused on other shards' keys or on calls over every shard; that transaction was already ended elsewhere and rolls back, and the owner's exit raises `TransactionAborted` (a `RuntimeError`). A FanoutCache isn't a `TransactionalCacheLike`; it and `NullCache` are `ShardedCacheLike`.
- Why: holding every shard's lock would serialize the whole cache behind one transaction, which is what sharding exists to avoid, and taking N locks in order is N times the waiting. A read on another shard is refused too because it would see a value that the transaction's own writes can't be atomic with, so a block that mixes shards is a mistake either way, best caught on the first call. A call over every shard would run inside the transaction on one shard and outside it on the rest, which is the same mistake (Michael, 2026-11-06). The check costs one relaxed load of a process-wide count of open transactions, but only while no transaction is open anywhere in the process: one long `Cache.transact()` on any thread sends every thread's keyed FanoutCache calls, calls over every shard and iterator `next()`s down the slow path, which is still cheap (it stops at once for a shard with no transaction, and costs a gate entry and a mutex lock per shard that has one). In a child forked while transactions were open the count can stay above zero for good, so that child always takes the slow path: speed only, never correctness.
- Tests: `tests/conformance/test_fanout.py` (`test_a_write_to_another_shard_doesnt_wait_for_a_transaction` is a strict xfail on diskcache), `tests/unit/test_fanout_transact.py`

## D-041 Your own types are registered per type, not through a `Disk` subclass, and are never pickled

- Kind: note (pinned by `tests/unit/test_type_registry.py`)
- diskcache: a `Disk` subclass overrides `put`/`get`/`store`/`fetch` for every value of a cache, and sees whole values; unpickling runs whatever class the bytes name.
- oxide: `register_type(cls, name=..., encode=..., decode=...)` registers one type, process-wide (`diskcache_oxide.registry`), or per handle with `Cache(types=TypeRegistry())`, consulted first. A dataclass needs only a name: its `init=True` fields are stored and it is rebuilt with `cls(**fields)`. A registered value is written as `{"$t:<name>": body}` in both serializers, with the body encoded by the normal path (so it may hold other registered types). On read the name is looked up in the reading process's registry: an unregistered name is a `ForeignValueError` and the row stays, so nothing is ever imported by name. Lookup is by exact type (no MRO walk); the codec's own exact types can't be registered; last registration wins by class and by name. A value that reached a registration is never pickled, even on a `pickle=True` handle: an error inside it (the `encode` function, an unstorable field, a self-returning `encode`) raises from `set`. An error from a `decode` function is classified as a pickle hook's would be (`OSError`, `RecursionError`, `MemoryError`, `UnsafeValueError` and anything that isn't an `Exception` pass through with the row kept; a `CorruptValueError` from a nested read is raised unchanged and counts as this row's, so the outer row is deleted under `'miss'`), except that one about the row (a `TypeError` from an old-shape dataclass row, a `ValueError` from the function) becomes `CorruptValueError` with the original as `__cause__`, so under `on_decode_error='miss'` the row is deleted, and under `'raise'` the caller sees `CorruptValueError` where an unpickling error would have passed through unchanged. Decode runs with the connection held, as a pickle hook does; a decode function may re-enter the cache. Once anything is registered, a dict's entries are copied before they are encoded on every build (not only free-threaded), since a registered function can release the GIL mid-dict.
- Why: a `Disk` subclass is pickle-shaped (trusted with every row, called on every value), and the Rust encoder can't call back into one per value without giving up the fast path for built-ins. Per-type functions keep built-ins at zero cost (the lookup comes after every exact check), keep the read side an allowlist, and cover the types a dunder protocol can't reach, since most of what apps store that isn't a built-in is a type they don't own (a bson `ObjectId`, a namedtuple from a library, a `str` subclass). The row is deleted on a decode error because a miss already makes the app rewrite the key; keeping it would fail the decode on every read. The wrap into `CorruptValueError` means one type says "this build can't read this row" and the message names the tag (Michael and the architect, 2026-11-06, plan 16).

## D-042 Value files live one level deep, and empty value directories go when the cache closes

- Kind: test-visible
- diskcache: a large value's file lives at `aa/bb/<name>.val`, two levels of 256 directories each (65,536 in all), and `Disk.remove` calls `os.removedirs` after each file it deletes, so a directory goes as soon as its last file does. `Disk.store` retries its `makedirs` and `open` up to 10 times for the race that opens.
- oxide: a value file lives at `aa/<32 hex>.val`, one level, so a store never has more than 256 value directories (about 1 MiB on ext4, at most 2,048 for a default 8-shard FanoutCache). A delete removes only the file. Empty value directories are removed when the process releases its last handle on the cache (by `close()` or by a handle dropped without it), skipping any modified in the last 30 seconds so a busy directory isn't removed and re-created, and at most 100 ms of that per close (a placeholder until plan 17 measures it): a sweep cut short is finished by a later close. `clear()` removes the ones it emptied at once, with no age limit on the folders. Neither runs inside a held transaction, and a FanoutCache's shards aren't swept yet (plan 17, part 2). The sweep visits only entries named exactly two lowercase hex characters and never recurses; in each it removes the temp files a dead writer left (`<32 hex>.tmp` under its folder's prefix, a regular file unwritten for an hour; a live `.tmp` lasts one write and at most one `timeout`-bounded lock wait, and a young one may be another process's write in flight; the trade is that a live writer which goes an hour without touching its file, paused or waiting past an hour on the lock, loses it and its write raises `OSError` with nothing stored) and then calls `rmdir`, which removes nothing but an empty directory. It never touches a `.val`, whatever its age, since one with no row may be a held transaction's file. An empty folder with such a name that you leave in a cache directory (`db/`, say) may be removed. A write whose directory a sweep removes under it retries. One platform gap: on macOS, APFS can let `rmdir` succeed on a directory while a create inside it succeeds too (plan 19's probe, 2026-11-07: 2 orphaned files in 54,353 creates racing a tight `rmdir` loop, no oxide code involved), so a sweep in one process (a `clear()`, or a last close reaching an empty folder quiet for 30 seconds) racing another process's first large write into that folder can, very rarely, make that write fail with `OSError` (`ENOENT` or `EINVAL`) after its file was created; Linux serializes the two. The write isn't made to recover from it (plan 19 has the reasoning and the recipe if it ever shows in the soak). Either way, a plain `Cache` closed by its process's last handle, outside any transaction and with no fork pending, leaves none empty that went untouched for 30 seconds; another process holding it open sweeps at its own last close.
- Why: before this, oxide never removed a value directory at all, so a long-lived cache filled all 65,536: about 257 MiB of empty directories per store on ext4, 2 GiB for a default FanoutCache, measured on the soak (plan 17). With 256 directories the worst case is small enough that removing one on every delete isn't worth its cost (a race on every delete, up to four directory updates per large write and delete); the sweep keeps the count to the live files at a close's leisure (Michael, 2026-11-06).
- Tests: `tests/conformance/test_value_dirs.py`, the `spill::` and `store::` sweep tests in core

## D-043 A large write after the cache's directory was deleted under it fails

- Kind: test-visible
- diskcache: `Disk.store`'s `os.makedirs` is recursive, so a large `set` on a handle whose cache directory was deleted re-creates the directory tree and succeeds.
- oxide: value directories are created one level at a time inside the cache directory, which is never re-created: such a write raises `OSError` (`ENOENT`, naming the path) after one `mkdir`. A small `set` still succeeds, into the database file the process already has open, as it does on diskcache.
- Why: re-creating the tree under a deleted cache leaves orphan value files in a directory nothing will read (plan 15's note). Deleting a cache's directory while a process has it open isn't supported; delete it with every process stopped.
- Tests: `spill::tests::a_write_into_a_deleted_cache_directory_fails_at_once_and_creates_nothing`

## D-044 Keys are compared by exact type

- Kind: test-visible
- diskcache: keys are compared by SQLite, so `1` and `1.0` are one key (the second write replaces the first), while `True`, pickled, is another.
- oxide: `1`, `1.0` and `True` are three keys, as are `0` and `False`. `0.0` and `-0.0` are one key. Inside a tuple the same rule holds element by element.
- Why: a key's stored bytes are its identity and its FanoutCache shard, so they must follow from the value and its type alone, in every process and release. The value codec never coerces types either (Michael, 2026-11-10).
- Tests: `test_keys.py`

## D-045 `memoize(typed=True)` keys hold type names, not classes

- Kind: test-visible
- diskcache: `typed=True` appends each argument's class to the key tuple, and pickles the key.
- oxide: it appends each argument type's `'module.qualname'` string (`'builtins.int'`) where diskcache appends the class. An int and a float argument still make two entries.
- Why: a class can't be stored as a key without pickle, and keys are never pickled (D-004, D-001).
- Tests: `test_memoize.py`

## D-046 `iterkeys()` orders keys of different types by type first

- Kind: test-visible
- diskcache: `iterkeys()` follows SQLite's order over the stored keys: numbers (ints and floats together), then text, then BLOBs (bytes, and pickled keys in pickle-byte order).
- oxide: every `str` key first, in code-point order, then `None`, `False`, `True`, ints, floats, bytes and tuples, each type in value order; tuples compare element by element in the same order, and a tuple comes before any longer tuple it begins. `reverse=True` is exactly the reverse. `prefix=` walks `str` keys only. `iter(cache)` is insertion order for every key type, as before.
- Why: it is the order of the stored bytes, so it is an index scan, the same in every process, and FanoutCache's merge gives the same order as `Cache`.
- Tests: `tests/unit/test_keys.py`, `key::tests`

## D-047 `retry` is keyword-only, and dict syntax never retries

- Kind: test-visible
- diskcache: `retry` is a positional parameter of every call that takes it (`cache.set(k, v, None, False, None, True)` works), and `cache[k]`, `cache[k] = v` and `del cache[k]` pass `retry=True`, so they wait forever on a busy database.
- oxide: `retry=False` is keyword-only, a strict `bool`, on `get`, `set`, `add`, `delete`, `pop`, `touch`, `incr`, `decr`, `expire`, `cull` and `clear` (`Cache`, `FanoutCache`, `NullCache` and the protocols). With `retry=True` only a plain `Timeout` is retried, each round with the call's whole `timeout`; a durable handle then retries it rather than answering it as a miss. A retried `expire`, `cull` or `clear` keeps the snapshot it started from and returns everything every round removed (on a FanoutCache, from every shard). Dict syntax raises `Timeout` after `timeout`, as `Cache.get` does (D-034 says the same for FanoutCache). Only this cache's own `Timeout` is retried: one raised by the caller's code (a hook, a `clock`, a nested call) is raised at once. A retried `set` or `add` encodes its value once. A removal that couldn't take its snapshot at first takes it when it gets in, so writes made during the wait can be removed too. Inside a held transaction a retried call keeps the transaction, and its write lock, across rounds, as diskcache does.
- Why: a positional `retry` after `read` and `tag`, which oxide doesn't have, would bind the wrong argument; and an unbounded wait hidden behind `cache[k]` is the hang this library exists to remove. Ask for it with `retry=True`.
- Tests: `tests/conformance/test_retry.py`, `tests/unit/test_retry.py`

## D-048 A memoized function's arguments must be key types

- Kind: test-visible
- diskcache: a memoized call's key holds its arguments, pickled, so any picklable argument works: lists, dicts, sets, objects. A NaN argument pickles too, and hits.
- oxide: every argument not in `ignore` must be a key type (D-004), checked before the function runs: otherwise the call raises `TypeError`, or `ValueError` for a NaN or a tuple nested too deep, naming the argument (`memoize: argument 0 can not be part of a cache key: ...`). `-0.0` and `0.0` are one argument, as for `functools.lru_cache`, so a sign-sensitive function gets `f(0.0)`'s result for `f(-0.0)`. `async def` functions are refused when decorated. On a durable handle the reads and writes don't pass `retry=True`, so an outage runs the function and drops the store.
- Why: keys are never pickled (D-004, D-001). Pass a list as a tuple, or leave an argument that can't be a key out with `ignore=` (a connection, a client) and let the others identify the call.
- Tests: `tests/unit/test_memoize.py`

## D-049 `stats()` counts per process, in memory

- Kind: test-visible
- diskcache: hits and misses are stored in the database's `Settings` table, shared by every process and kept across restarts; with statistics on, every `get` becomes a write transaction to count it.
- oxide: counted in memory, per process: every handle on a directory in one process shares the counts, they survive closing the last handle, a forked child and a restarted process start at zero, and other processes' lookups aren't included. Counted by `get` and `cache[key]` only (as diskcache); a durable outage answered as a miss counts as nothing. A `FanoutCache` sums its shards. `statistics=` isn't a constructor setting; `stats(enable=...)` turns counting on and off.
- Why: a `get` that writes would queue every reader behind the write lock, and reads that never write are what let many processes read at once (Michael's decision 7, plan 24). Sum the workers' counts yourself, or through `Caches`.
- Tests: `tests/conformance/test_parity.py`, `tests/unit/test_stats.py`

## D-050 `check()` doesn't report empty value folders, and keeps young files

- Kind: test-visible
- diskcache: `check()` warns about every empty directory (`EmptyDirWarning`) and every file no entry names (`UnknownFileWarning`), whatever its age, and `fix=True` removes them; a wrong-size value file has its row's size corrected to the file's.
- oxide: empty value folders aren't reported (they're normal on an open cache, D-042) and `fix=True` sweeps them; a file no entry names is reported, and removed by `fix=True`, only once it's over an hour old (a younger one may be a writer's in flight: its `.tmp`, or a renamed `.val` between a busy commit and its retry), and only inside a value folder is one ever removed; a wrong-size file means a crash cut it short, so `fix=True` removes the entry rather than serve the short bytes. The database's own files are never reported or touched. `check()` runs in short steps, without holding the write lock across the scan.
- Why: a `check(fix=True)` that deleted a live writer's file, or blessed a truncated value, would cause the damage it's meant to find (Michael's decision 20, plan 24).
- Tests: `tests/conformance/test_parity.py`, `check::tests`

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
- **D-034 (a FanoutCache's single-key calls swallow `Timeout`):** the fork has the same code: with every shard write-locked by another process, `set`, `add`, `delete` and `touch` give False and `incr` and `pop` give None, so the six `test_a_write_to_a_locked_shard_raises_timeout` cases fail on community.
- **D-037 (FanoutCache connections per thread per shard):** the fork also keeps one connection per thread per shard: 264 fds inside the root with 64 threads on 8 shards, the same as 5.6.3, so `test_peak_fds_bounded_by_the_shard_budget` fails on community.
- **D-018 on a FanoutCache:** fixed in the fork, as for `Cache`. After 64 threads and `close()`, no fds remain inside the root, so `test_close_releases_every_fd_on_every_shard` passes on community.
- **D-040 (FanoutCache.transact() locks every shard):** the fork has the same code: a write from another thread to another shard during a transaction waits out the timeout and gives False, so `test_a_write_to_another_shard_doesnt_wait_for_a_transaction` fails on community.
- Everything else in the suite passes on the fork unchanged.
