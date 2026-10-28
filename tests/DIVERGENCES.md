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
- Tests: `test_processes.py` (`test_fork_while_another_thread_holds_a_write_transaction` is a strict xfail on diskcache; `test_fork_while_another_thread_writes_the_cache` is oxide-only)

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
