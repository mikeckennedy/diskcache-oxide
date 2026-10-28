# Divergences from diskcache 5.6.3

Every intentional behavioral difference between diskcache-oxide and diskcache 5.6.3. Each entry says whether it is **test-visible** (a conformance test carries `@pytest.mark.diverges(ref=...)` for it, or a `backend_only("oxide")` test asserts the new behavior) or a **note** (hidden by the test fixtures, but users should know about it).

A divergence is a deliberate decision. It is never a way to skip a failing test.

## D-001 Unrepresentable values raise TypeError by default

- Kind: test-visible
- diskcache: pickles every value, so anything picklable round-trips.
- oxide: stores `str`/`bytes` raw and structured values as JSON (with tags) or msgpack. With the default `pickle=False`, a value the chosen format can't represent exactly raises `TypeError` at `set`. Passing `pickle=True` restores the pickle fallback for that cache.
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

## D-011 clear() removes what existed when it started

- Kind: note (proposed; to be pinned down with a test in Phase 2)
- diskcache: `clear()` keeps deleting until the cache is empty, including rows written while it runs, so a writer that never stops can keep it running indefinitely. Found by the Phase 0 locking tests.
- oxide: `clear()` removes the entries that existed when it started (bounded by a rowid snapshot) and always finishes. Writes that land during the clear survive.
- Why: a maintenance call must always terminate.
