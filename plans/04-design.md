# diskcache-oxide design (Phases 2-3)

Parent: [00-roadmap.md](00-roadmap.md). Evidence: [03-phase-1-foundation-spikes.md](03-phase-1-foundation-spikes.md) (Spike 1 and 2 results), [spike-3-codecs.md](spike-3-codecs.md), [spike-4a-storage.md](spike-4a-storage.md). Behavior contract: the conformance suite in `tests/conformance/` plus [tests/DIVERGENCES.md](../tests/DIVERGENCES.md).

Status: reviewed by the architect at the end of Phase 1 (corrections incorporated); GO for Phase 2.

This document records the decisions Phase 2 implements. It doesn't repeat the evidence; each section points to where it came from. When this document and the conformance suite disagree, the suite wins and this document gets fixed.

## 1. Layout

- `crates/diskcache-oxide-core`: the storage engine, with `#![forbid(unsafe_code)]` and no PyO3.
  - It deals in keys, modes, bytes and value files, plus the clock, the pool, the fork gate, maintenance and settings.
  - Modules: `error`, `sqlite`, `schema`, `settings`, `pool`, `gate`, `store` (get/set/add/delete/incr/...), `spill`, `maintenance` (expire/clear/cull), `scan`.
- `crates/diskcache-oxide-py`: the `diskcache_oxide._core` extension.
  - `Cache` is the `#[pyclass(frozen)]` itself, with no Python wrapper frame (Spike 2).
  - Modules: `cache` (the pyclass), `codec/{common,json,msgpack}` (promoted from `spikes/codec`), `errors`, `fork` (the `register_at_fork` hooks).
- `python/diskcache_oxide/`:
  - a typed `__init__.py` that re-exports `_core`;
  - `null.py` (`NullCache`) and `protocol.py` (`CacheLike`), both pure Python;
  - the hand-written `_core.pyi`, checked by `mypy.stubtest`;
  - `py.typed`.
- The spike modules (`spike.rs` in both crates) are deleted when Phase 2 lands. So are `spikes/codec` and `spikes/storage`, once their code is promoted. The spike workspaces stay in git history.

## 2. Python API (Phase 2)

```python
class Cache:
    def __new__(
        cls,
        directory: str | os.PathLike[str],
        timeout: float = 60.0,
        *,
        size_limit: int | None = None,   # None: keep the stored value (default 2**30 for a new cache)
        cull_limit: int | None = None,   # None: keep the stored value (default 10)
        serializer: Literal['msgpack', 'json'] = 'msgpack',
        pickle: bool = False,
        clock: Callable[[], float] | None = None,
        max_connections: int = 8,
        sqlite_cache_size: int = 256,  # pages per connection (added in Phase 2 step 9, D-023)
    ) -> Cache: ...
```

- Only `directory` and `timeout` may be positional, matching how diskcache is called in practice. Everything else is keyword-only.
- **Persisted settings use `None` sentinels.** Only values the caller actually passes override what's stored. A reopen with no `size_limit` keeps the stored one, as `test_size_limit_persists_but_timeout_does_not` requires; a plain `2**30` default couldn't tell "not passed" from "passed the default".
- **Deliberate API divergences**, each needing a `DIVERGENCES.md` entry (D-021, D-022):
  - `directory` is required. diskcache's default `None` creates a temporary directory.
  - `disk` and `eviction_policy` aren't accepted. diskcache takes `disk` positionally. Other eviction policies arrive in Phase 6a.
- **Validation:**
  - `timeout`, `size_limit` and `cull_limit` reject `bool` with `TypeError` (D-014).
  - `size_limit` below `MIN_SIZE_LIMIT` (1 MiB) raises `ValueError` (D-005).
  - `max_connections` must be at least 1.
- **Methods** (diskcache semantics unless a divergence says otherwise):
  - `get(key, default=None, *, expire_time=False)`. With `expire_time=True` it returns `(value, expire_time)` or `(default, None)`, as 13 ungated conformance tests require. `read=` and `tag=` arrive in Phase 6a.
  - `set(key, value, expire=None)`, `add(key, value, expire=None)`, `delete(key)`;
  - `incr(key, delta=1, default=0)`, `decr(...)`, `touch(key, expire=None)`, `pop(key, default=None)`;
  - `__getitem__`, `__setitem__`, `__delitem__`, `__contains__`, `__len__`, `__bool__` (always True, D-009);
  - `__iter__` (insertion order), `iterkeys(*, prefix=None, reverse=False)` (key order);
  - `expire()`, `clear()`, `cull()`, `volume()`, `close()`, `__enter__`/`__exit__`;
  - read-only attributes `directory`, `timeout` (this handle's), `size_limit` and `cull_limit` (the effective stored values), and `enabled` (always True);
  - `_hold_write_transaction()` (a private test hook until `transact()` lands in Phase 6b).
- `expire` accepts `int`/`float` seconds or `None`, and rejects `bool` (D-014). Keys are `str` only (D-004).
- **Errors:**
  - `diskcache_oxide.Timeout(TimeoutError)` when a lock wait or pool wait runs out. `get` can raise it too, through the spilled-value locked fallback (section 4) and pool waits.
  - `UnsafeValueError(TypeError)` when a `pickle=False` cache reads a pickled row (D-001).
  - `TypeError` for unsupported values, keys or argument types.
  - `ValueError` for bad settings.
  - `OSError` (with errno) for disk-full and I/O errors.
  - `FormatMismatchError(RuntimeError)` for a database in a newer format.
  - `RuntimeError` for a transaction poisoned by fork, or ended from inside one of its own operations (a pickle hook leaving the `with` block). The latter is rolled back once that operation returns. A held transaction's connection is lent to each of its owner thread's operations, so a hook that calls the cache from inside one runs inside the transaction too (review 07).
  - Corrupt rows raise `CorruptValueError(ValueError)` and never a raw serde message as the exception type.
  - All of these are created with `create_exception!(diskcache_oxide, ...)` so they pickle.
- **`NullCache` and `CacheLike`:** as specified in the roadmap (instant expiry, `invalidate=`, the same constructor signature, `enabled` is False, falsy). A test asserts that `Cache`, `NullCache` and `CacheLike` have the same public signatures.

## 3. On-disk format (format version 1)

File: `<directory>/diskcache-oxide.sqlite` (D-002), with `-wal` and `-shm` files alongside. Value files live under `<directory>/aa/bb/<32 hex>.val`.

```sql
CREATE TABLE cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,  -- insertion order; __iter__ uses it; kept across overwrites; never reused
    key_type INTEGER NOT NULL,     -- 1 = str (Phase 2); others reserved for Phase 6a
    key TEXT NOT NULL,
    mode INTEGER NOT NULL,         -- see section 5
    value BLOB,                    -- inline value; NULL when spilled
    filename TEXT,                 -- 'aa/bb/<32hex>.val' when spilled
    size INTEGER NOT NULL,         -- bytes of value or file, for volume()
    store_time REAL NOT NULL,
    expire_time REAL,              -- NULL = never
    store_seq INTEGER NOT NULL     -- monotonic per stored version; eviction and maintenance bounds
);
CREATE UNIQUE INDEX cache_key ON cache (key_type, key);
CREATE INDEX cache_store_seq ON cache (store_seq);
CREATE INDEX cache_expire ON cache (expire_time) WHERE expire_time IS NOT NULL;
CREATE INDEX cache_filename ON cache (filename) WHERE filename IS NOT NULL;
CREATE TABLE settings (name TEXT PRIMARY KEY, value) WITHOUT ROWID;
-- Exactly one row. Updated in the same transaction as every row change.
CREATE TABLE totals (only INTEGER PRIMARY KEY CHECK (only = 1), count INTEGER NOT NULL, size INTEGER NOT NULL,
                     next_seq INTEGER NOT NULL, size_limit INTEGER NOT NULL, cull_limit INTEGER NOT NULL);
-- PRAGMA user_version = 1
```

- **`store_seq`** comes from `totals.next_seq`, which every write increments in its transaction.
  - It is *not* `MAX(store_seq) + 1`, as spike 4a did. That reuses a number once the newest row is deleted or expired, so a later write could get a sequence at or below a running `clear()`'s snapshot and be wrongly deleted (D-011).
  - `id` uses `AUTOINCREMENT` for the same reason: insertion order must never reuse a deleted row's id.
  - `(id, store_seq)` names exactly one stored version of one row.
- **Running totals** (`totals.count`, `totals.size`) are updated in the same transaction as every row change, so they are atomic and crash-safe. `__len__` and `volume()` stay O(1); the Phase 0 baselines measured diskcache's at about 0 ms for 400k keys, and we must not regress to `COUNT(*)`.
  - **Every row change goes through one Rust choke point** (`store::RowWriter`). That includes the D-013 compare-and-delete, culling, `expire()`/`clear()` chunks and overwrites. Nothing outside it issues `INSERT`/`UPDATE`/`DELETE` against `cache`.
  - A property test runs random operations, then recomputes `COUNT(*)` and `SUM(size)` and compares them with `totals`.
  - `volume()` is `page_count × page_size + totals.size`, the same idea as diskcache's. The exact numbers differ (D-006).
  - `len` counts expired-but-unswept rows, as diskcache does.
- **Persisted settings:** `size_limit` and `cull_limit`, stored in the `totals` row so each write transaction reads the current values. As in diskcache, these persist, and only values a caller explicitly passes overwrite them, so the last explicit opener wins. `serializer`, `timeout`, `pickle`, `clock`, `max_connections` and `sqlite_cache_size` are per handle and never persisted. Reads decode by each row's mode, so handles with different serializers interoperate.
- **Open sequence** (it writes nothing when everything already matches, D-019):
  1. Create the directory with 0700 if it's missing (D-015); value-file subdirectories are also 0700.
  2. Open the connection with the custom busy handler (section 7).
  3. Read `PRAGMA journal_mode`. Only if it isn't `wal`, set it to WAL and verify the result, raising `NotWal` on failure. This happens *before* any schema transaction, so a new database is created directly in WAL mode.
  4. Read `PRAGMA user_version`.
     - 0 means an empty or new database: create the schema and the `totals` row and set the version inside one `BEGIN IMMEDIATE`, re-checking `user_version` under the lock so racing creators are safe (Test B).
     - A version other than ours raises `FormatMismatchError` and never writes.
     - **There are no in-place migrations.** Old and new workers overlap during deploys, so a format change instead gets a new database filename (`diskcache-oxide.v2.sqlite`). Caches are disposable, so the new format starts cold beside the old one, and an old binary never sees the new file. `user_version` stays as a safety check against a mislabeled file.
  5. Read `settings`. Write only the explicitly passed values that differ from what's stored.
  6. `PRAGMA synchronous = NORMAL`. This is a connection setting and writes nothing. WAL + NORMAL survives application crashes and SIGKILL (spike 4a, 600 kills) but not power loss, as with diskcache.
  7. Never run `PRAGMA optimize` on close.

## 4. Large values (spill protocol)

- **Threshold: 64 KiB.** A `str`/`bytes` raw value, or an encoded value, of 64 KiB or more is stored in a file. The conformance adapter's `spill_threshold` becomes 65536 (spike 4a).
- **Write:**
  1. Write `aa/bb/<random 128-bit hex>.tmp` (`create_new`, no fsync) with no lock and no connection held, and with the interpreter released.
  2. `BEGIN IMMEDIATE`; read the old filename for the key; rename `.tmp` → `.val` while holding the lock; upsert the row with a new `store_seq`; update the totals; `COMMIT`.
  3. Unlink the old file after the commit.
  4. On error before the commit, roll back and unlink both names. Names are never reused.
- **Read:**
  1. Run the autocommit `SELECT`, then open and read the file with no transaction held and the interpreter released.
  2. Check that the file's length equals the row's `size`. With `synchronous = NORMAL` and no fsync of value files, a crash can leave a short or empty file, and a mismatch is treated as a missing file.
  3. On ENOENT or a length mismatch, re-read the row.
     - If it's gone, that's a miss.
     - If `filename`/`store_seq` changed, retry.
     - If it's unchanged, it's a true miss. Run a compare-and-delete (`WHERE id = ? AND store_seq = ?`, through `RowWriter` so the totals stay right) with a zero busy timeout, swallow BUSY, and return the default (D-013).
  4. After 8 optimistic attempts, fall back to reading the row and opening the file under `BEGIN IMMEDIATE`, then read from the fd after the commit. This fixes the livelock spike 4a found.
     - The fallback means **`get` can raise `Timeout`**, and the API docs say so.
     - In a D-020 child whose database is unwritable, the fallback (and the compare-and-delete) is skipped: the read keeps retrying optimistically up to the deadline, then returns the default.
     - A Phase 2 step-1 unit test confirms that an empty `BEGIN IMMEDIATE`/`COMMIT` doesn't bump a peer's `data_version` (D-019).
     - If that test fails, or the fallback proves costly, the alternative keeps reads lock-free: writers move replaced files to a graveyard directory and sweep it after a grace period, so a row's file can't vanish under a reader.
- **Inline writes and deletes** use the same transaction shape: read the old filename, change the row, commit, then unlink.
- **`check()` (Phase 6a):** finding orphan `.val` files is exact (a `.val` that no row references, seen while holding the write lock). `.tmp` files are removed only once they're older than a grace period.

## 5. Values and codecs

**Modes**
- 1 = raw `str` (UTF-8);
- 2 = raw `bytes`;
- 3 = msgpack;
- 4 = JSON;
- 5 = pickle;
- 6 = SQLite-native integer;
- 7 = SQLite-native float.

**Native numbers**
- Mode 6 holds exact `int` values that fit in 64 bits (not `bool`).
- Mode 7 holds exact finite `float` values. NaN and ±inf stay in the structured serializer, because SQLite binds NaN as NULL (D-008). A unit test confirms `-0.0` round-trips through REAL; if it doesn't, `-0.0` also goes through the serializer.

**`incr` and `decr`**
- They work on modes 6 and 7 (`test_incr_on_float`, `test_incr_float_delta_on_missing_key`).
- Implementation: read the value, compute with `checked_add` (ints) or plain addition (floats) in Rust, then write it back, all inside one `BEGIN IMMEDIATE`. The transaction makes it atomic across processes.
- Not `UPDATE ... SET value = value + ?`: SQLite silently turns an overflowing integer into a REAL. Ours raises `OverflowError` past 64 bits and leaves the value unchanged, as diskcache does (pinned in Phase 0).
- On a non-numeric value it raises `TypeError` (`incr` on `True` is a `TypeError`, as pinned).
- **`incr` counts as a store.** It bumps `store_seq` and `store_time`, matching diskcache, which rewrites `store_time` on `incr`. So a frequently incremented counter isn't the first thing evicted. Phase 2 adds a test that pins this.

**Structured values** use the cache's `serializer`:
- `msgpack` is the default (Michael, 2026-10-28), because Spike 3 found it fastest and smallest everywhere.
- `json` uses serde_json with `float_roundtrip`.
- Reads decode by the row's mode, whatever the current setting.

**Tag format.** Exactly as specified in spike-3-codecs.md ("Tag format spec"):
- exact types only, and subclasses raise `TypeError`;
- `$tuple`, `$set`, `$frozenset`, `$int` (beyond 64 bits), `$date`, `$decimal`, `$datetime` (RFC 9557-style text, including `[_fold=1]` and ZoneInfo keys), and `$esc` for user dicts that look like tags;
- in JSON only: `$bytes`, `$dict` (non-str keys) and `$float` (NaN/inf);
- msgpack uses the timestamp extension for UTC datetimes.

**Pickle.** Only with `pickle=True`, and only as a fallback when the serializer can't represent a value. A `pickle=False` handle that reads a mode-5 row raises `UnsafeValueError` and leaves the row in place.

**Encoding**
- Encoding happens before a connection is taken.
- The encoder calls only C-implemented methods (`isoformat`, `str` of `Decimal`/big ints, `tzname`, `ZoneInfo.key`) and never user code, because subclasses and custom tzinfo are rejected first.
- The output buffer depends on size: a reused per-thread buffer for small values, writing directly into `PyBytes` for large ones (Spike 3).

**Decoding**
- Small values are decoded while the connection is held, straight from SQLite's row buffer. This runs no user Python (Spike 2).
- Spilled values are decoded after the file read, with no connection held.
- A per-thread dict-key string cache speeds up decoding (10-18%). When promoting it, use `try_borrow_mut` on the cache (`KEYS`, spikes/codec/src/common.rs:122), as the scratch buffer already does, so a re-entrant decode falls back instead of panicking.

**Compatibility rule for codec changes (plan 08, 2026-10-30).** A new mode, tag or msgpack extension can be added without a format change only because an older build reading it raises `ForeignValueError` (a `CorruptValueError` subclass). Under `on_decode_error='miss'`, that's a miss that leaves the row in place, so an old worker overlapping a deploy never deletes a newer worker's data. The same goes for a zone key missing from a host's tzdata. A change that older builds must not read at all (new bytes for an existing mode or tag) needs a new database filename instead, as in section 3.

**Fidelity contract:** the value table in `tests/conformance/value_cases.py`, where every case is now `roundtrip` or `TypeError`, plus the Hypothesis round-trip tests promoted from `spikes/codec/tests`.

## 6. Expiry, eviction, culling, maintenance

- **Clock.** `clock=` (if given) is called exactly once per operation, before a connection is taken. Otherwise the native `SystemTime` is read. The busy handler always uses the native clock.
- **Expiry** follows diskcache's pinned semantics:
  - a miss at or after `expire_time`;
  - `expire=0` or a negative value is an immediate miss;
  - `add` succeeds over an expired key;
  - `incr` keeps the existing expiry, and `incr` on an expired or missing key creates a counter with no expiry.
- **Eviction.** Least-recently-stored by `store_seq`, so `get` never writes.
- **Culling on `set`/`add`/`incr`**, with one shared budget of `cull_limit` rows per call, as in diskcache:
  1. Remove up to `cull_limit` expired rows, ordered by `expire_time`.
  2. Only if `volume() >= size_limit`, remove up to `cull_limit − removed` more, ordered by `store_seq`. One call never removes more than `cull_limit` rows in total.
  3. Unlink their files after the commit.

  This matches the pinned bounded-culling behavior, which is gated `requires('cull_limit')` in the suite. Phase 2 adds `cull_limit` to `OXIDE_SUPPORTED_FEATURES`.
- **`expire()` and `clear()`:**
  - work in chunks of 100 rows ordered by `store_seq`, bounded by a `store_seq` snapshot taken at the start, so they always finish and writes made during them survive (D-010, D-011);
  - unlink files after each chunk's commit;
  - pause 100 µs with the interpreter released between chunks;
  - return exact counts.

## 7. Concurrency

**Pool**
- Up to `max_connections` (default 8) connections per database file, shared by every handle on that file through the process-wide `Store`. The cap is the largest value any live handle asked for. The connections are kept behind an uncontended `Mutex<Vec<_>>`.
- Memory: each connection's page cache is `sqlite_cache_size` pages (default 256, 1 MiB; the largest any handle asked for), applied when a connection is leased. A database file's SQLite memory is therefore bounded by about `max_connections` × (pages × about 5 KiB + about 330 KiB of fixed overhead per connection). `tests/unit/test_memory.py` holds the bound, and the `get_random_wide` benchmark measures its cost (D-023).
- WAL size (D-024, Phase 2.5 step 1): `wal_size_limit` (default 64 MiB) sets `journal_size_limit` to half the limit and scales `wal_autocheckpoint` from it; both are free. Forced checkpoints (PASSIVE, then TRUNCATE repeated while busy, up to 8 tries, each in a fresh 2 ms window, with the interpreter released; plan 09, step 2b) are manual by default: `Store::checkpoint(timeout)` / `Cache.checkpoint()`, and `close(checkpoint=True)` when the WAL is past its trim size, waiting at most 1 s. `wal_checkpoint="auto"` opts in to running them after writes (every 250 ms or 64 writes, only while over the limit, 1 s backoff when busy). Outcomes are counted (`Store::wal_stats`, `Cache._wal_stats()`). Taking SQLite's own autocheckpoint off the write path entirely is the background maintenance thread's job.
- Two builds: the shipped one compiles SQLite with `SQLITE_DEFAULT_MEMSTATUS=0`, since the global memory statistics take a process-wide mutex on every allocation (2-10% of threaded throughput, most on 3.14t). Performance numbers come from it. A memory-instrumented build (`.venv-mem`: 3.14t, statistics on, a non-editable install with its own target dir) runs the memory tests and `bench/memory.py`. Its memory numbers are definitive, and its throughput is informational only.
- A checkout tries without blocking first. If none is free and the pool is at its cap:
  - **At gate depth 0** (the usual case), it waits *before* entering the gate, polling every 1 ms with the interpreter released and bounded by `timeout`, and raises `Timeout` when that runs out. A pool `Condvar` isn't used, because a child would inherit its parent's waiters.
  - **At depth > 0** (a nested operation inside a held transaction, which can't release the gate), it opens a connection over the cap instead of waiting.
- `PooledConn<'g>` borrows the `SharedGuard` and checks itself back in when dropped. With a cap, a missed checkin would permanently lose capacity, and the lifetime also makes "a connection is only used under the gate" a compile-time fact.
- Connections are tagged with the process generation (section 8).
- The fd budget is about 2 × `max_connections` + 1 + the value-file fds in flight (D-017). `close()` closes every idle connection, and in-flight connections close when they're returned (D-018).

**Waiting rules**
- Inline reads and small writes do **not** release the interpreter. It costs 15-20% of multi-thread throughput on GIL builds (Spike 2).
- Every *blocking* wait releases it, and none happens while the gate is held: pool waits, lock waits, maintenance pauses and value-file I/O.

**Lock waits (the busy handler).** SQLite calls the busy handler while the thread is still attached to the interpreter and still holds the gate. If it slept there for up to `timeout`:
- one cross-process lock wait would freeze every thread on a GIL build for up to 60 s;
- on 3.14t it would stall stop-the-world GC;
- it would deadlock with a fork: thread A holds a transaction and forks, thread B is busy-waiting on A's lock, `begin_fork` waits for B, and B waits for A.

So waiting is split in two:
1. **Short attached attempt.** Each connection's busy handler reads a per-connection `Arc<AtomicU64>` deadline that the operation sets to about 2 ms from now. Within that it polls on a fixed schedule: 10 µs, doubling to 100 µs.
2. **Detached retry.** If SQLite still returns `SQLITE_BUSY`:
   - roll back any transaction;
   - check the connection back in and leave the gate;
   - sleep with the interpreter released (100 µs, rising to 1 ms after 20 ms of waiting);
   - re-enter the gate, check out a connection and retry from the start (`BEGIN IMMEDIATE` for writes);
   - stop at the operation's `timeout` deadline and raise `Timeout`.

   Operations are written to be restartable, so retrying from the start is safe: encoding is done before the transaction, and file writes are idempotent temp files.
3. **One set of constants** lives in `core::sqlite`, which settles the 100 µs (spike 4a) vs 200 µs (Spike 5) question. Phase 2 tests cover it: a GIL-freeze test (another thread keeps running during a long cross-process lock wait) and a fork-latency test (a fork completes while a thread waits on a lock).

**No Python while a connection is held,** with these exceptions:
- decoding a small value from the row buffer, which runs no user code;
- `transact()` in Phase 6b, which pins a connection for the whole block.

Iteration fetches chunks of 512 keys and returns the connection between chunks.

**Lock order:** the in-process write lock (writes only), then the gate in shared mode, then pool checkout. No blocking wait on the write lock or the pool happens while holding the gate. At depth > 0, both are `try_*` only. The full rules, including the atomic generation-tagged write lock, `WriteToken`, fairness and the `Store` registry, are in [05-phase-2-core-cache.md](05-phase-2-core-cache.md), "Concurrency rules".

**Free-threaded scaling (investigated 2026-10-28, after Michael asked about connections and the read/write mix).** The Spike 2 throughput numbers were misleading, for both diskcache and oxide. The benchmark loop shared key strings and a stop flag across threads, so on 3.14t it was itself contention-bound: even a plain dict lookup got slower with more threads. With a share-nothing loop (3.14t, read-only, kops/s for 1 / 2 / 4 / 8 threads; this machine has 6 performance cores):

| Operation | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| Python dict lookup | 41,554 | 81,838 | 161,024 | 213,663 |
| no-op call into `_core` | 32,279 | 13,834 | 13,457 | 10,629 |
| one shared `Cache.get` | 987 | 574 | 878 | 927 |
| per-thread `Cache`, same directory | 976 | 549 | 1,063 | 1,272 |
| per-thread `Cache`, own directory | 1,008 | 1,347 | 1,754 | 705 |

With 10% writes, the old loop went from 397k (1 thread) to 144k (8 threads).

At the standard **99:1** mix (share-nothing loop, one shared `Cache`, kops/s for 1 / 2 / 4 / 8 threads):

| Backend | 1 | 2 | 4 | 8 |
|---|---|---|---|---|
| diskcache, 3.14 (GIL) | 430 | 254 | 81 | 20 |
| oxide, 3.14 (GIL) | 890 | 878 | 896 | 883 |
| diskcache, 3.14t | 448 | 417 | 449 | 479 |
| oxide, 3.14t | 878 | 674 | 791 | 601 |

- On the GIL build, diskcache collapses 20× from 1 to 8 threads, even at 1% writes. The likely cause is that each thread has its own connection, and a thread that loses a lock race sleeps in SQLite's millisecond-scale busy handler while holding the GIL. Oxide stays flat.
- On 3.14t, oxide is 1.3-2× faster than diskcache at every thread count, but it doesn't scale up yet (conclusion 1 below).

Conclusions:
1. **Calls through shared extension objects don't scale.** A no-op call gets 3× slower with more threads, most likely because of contended reference counts on shared objects (the module's function objects, the shared `Cache` instance).
2. **The shared `-shm` file is a secondary limit.** Per-thread `Cache`s on separate directories scale furthest.
3. **Writes collapse because same-process threads race on SQLite's write lock** and retry on a busy-handler interval far longer than a write.

Actions:
- **Phase 2:**
  - Rewrite `bench/baseline.py`'s threaded workload as a share-nothing loop (per-thread key strings, the method bound once, the stop flag checked every 1,000 operations), at **99 reads : 1 write**, which is the standard (headline) mix for all threaded benchmarks (Michael, 2026-10-28). At 90/10 the workload is borderline for caching at all. Also report a **1,000 : 1 "Talk Python profile"** alongside it, since much of Talk Python's cache traffic is at that ratio or higher. Report 1, 2, 4 and 8 threads, and re-baseline diskcache.
  - Add an **in-process write lock per database file**. Threads of one process queue on it, with the interpreter released and bounded by `timeout`, before `BEGIN IMMEDIATE`. Only other processes then meet SQLite's busy handler.
  - Build `Cache` as a thin handle over a process-wide store shared by every handle on the same file (pool, write lock, settings), so extra handles are cheap.
- **Phase 4 (top target, from Phase 2 profiling):** about 60% of a cached `get` is `__fcntl`. SQLite's unix VFS takes and releases a POSIX lock on a WAL read-mark slot in the `-shm` file for every read transaction (`walTryBeginRead` → `unixShmLock` → `fcntl`), under a process-wide `unixShmNode` mutex. diskcache pays the same cost, but it's also the most likely reason free-threaded reads don't scale: every reader thread serializes on that mutex and syscall. Candidates:
  - a VFS shim that caches `-shm` read-lock state per process, so that only the first and last holder of a slot make the system call (SQLite already aggregates within a process; the cost is that each read transaction is the first and last holder);
  - longer-lived read snapshots per connection, bounded so they don't starve checkpoints;
  - measuring OFD locks (`F_OFD_SETLK`) on Linux.

  Any of these must keep multi-process correctness, and the conformance and fork suites decide.
- **Phase 4:**
  - Try CPython 3.14's deferred reference counting (`PyUnstable_Object_EnableDeferredRefcount`) on `Cache` instances. It needs `unsafe` in the py crate, behind a local `allow`.
  - Profile the no-op call path.
  - Measure `SQLITE_DEFAULT_MEMSTATUS=0` / `SQLITE_THREADSAFE=2` again with the fixed benchmark.
  - Look at the `-shm` lock path.

## 8. Fork safety

Proven in Spike 5 (`crates/diskcache-oxide-core/src/{gate,pool}.rs`, and the hooks in `crates/diskcache-oxide-py/src/lib.rs`).

All 9 Phase 0.5 fork tests passed 20/20 with `--backend=oxide` on 3.14 and 3.14t: the deterministic D-012 holder test, both hammer tests, C1, C2 and the idle tests. A stress run with 8 threads on get/set/incr and 50 forks passed with exact counters and no hangs, including on 3.14t with a parallel `gc.collect()` loop (40k collections, no stall). `get` was unchanged, at 0.80 µs.

- **Hooks:** `os.register_at_fork` hooks are registered in the `#[pymodule]` init, not `pthread_atfork`. The before-hook waits with the interpreter released. CPython runs `after_in_child` hooks in registration order, so another library's hook registered before the import can use a cache in the child before ours runs. The forking thread's first gate entry in the child (its `forking` flag still set, `getpid()` no longer the forker's) therefore runs the child-side reset itself, and the after-hook then does nothing (2026-10-29, review 07; without it, such a hook wrote straight into a transaction the parent forked inside).
- **Gate:**
  - a custom gate with an atomic holder count, an atomic fork-pending claim and a thread-local depth, so shared entry is reentrant;
  - there's no mutex or condvar anywhere in it. A fork claims the gate with a compare-and-swap on `pending`, so concurrent forks take turns, and nothing is locked across `fork()`;
  - `begin_fork` waits until the holder count equals the calling thread's own depth;
  - every SQLite call, including opening and closing connections, holds the gate in shared mode;
  - pool methods take a `&SharedGuard` as compile-time proof that the gate is held.
- **Waiting:** every wait is a 1 ms sleep-poll with the interpreter released, both for the fork and for blocked entrants. Threads leaving the gate never notify anyone. With no mutex or condvar, a child, or a child that forks again, inherits no locked state and no waiter lists from the gate.
- **Lock order:** the gate first, then the pool. Spike 5 found this simpler than the order the review proposed, and it's safe because checkout never blocks while the gate is held.
  - Phase 2's bounded pool wait (at `max_connections`) must therefore happen *before* entering the gate, or release and re-enter it, so a fork is never held up by a thread waiting for a connection.
- **The `Store` registry and the write lock:** the registry mutex is only taken under the gate and is quiesced in `begin_fork`, like the pool registry. The write lock is atomic and generation-tagged, so a child resets it instead of inheriting a held lock.
- **Pool mutexes:** they're only taken under the shared gate, so none can be locked at fork time; `begin_fork` locks and releases each one as a check. They aren't held across the fork, because safe Rust can't hold std guards across the hooks through a `Weak` registry.
- **Generation:** a `GENERATION` atomic is bumped in the child. A pool that sees a stale generation `mem::forget`s its idle connections and resets its checked-out count. A stale connection that comes back is forgotten, never closed.
- **Idle connections are closed before the fork** (2026-10-29, review 07). `begin_fork` closes every pool's idle connections after quiescing. SQLite keeps one lock record (`unixInodeInfo`) per file per process, and a new connection that finds it recording SHARED just bumps a count and takes no `fcntl` lock. A child that inherited an open connection's record therefore held no kernel locks on that database at all, and once the parent closed, an unrelated process's last close checkpointed and deleted the WAL and shm under it, losing the child's writes (`parent-closes-after-fork` in `tests/unit/fork_scenarios.py`). With nothing open, SQLite frees the record and the child starts clean. Only the forking thread's own leases and held transactions still cross the fork; a held transaction's database is unwritable in the child anyway (below). The cost is reopening connections in the parent after each fork.
- **Busy handler:** a deadline-based handler retries at most 200 µs apart (section 7 uses 100 µs). SQLite's default handler retries only about 20 times a second, so a forked child's first write timed out in 1-4% of forks under a parent's write hammer. With the custom handler that dropped to 0 in 800 forks.
- **Forking inside your own transaction (D-020):** CPython can't be stopped from forking.
  - The child's inherited transaction is poisoned: operations inside it and its exit raise `RuntimeError`, and nothing commits.
  - SQLite's per-process lock table, inherited by the child, still records the write lock, and releasing it in the child would corrupt the parent's transaction. So **the child can never write to that database**.
  - Decision (2026-10-28): fail fast. In such a child, writes to that database raise `RuntimeError` at once ("this database can't be written in a process forked inside a transaction"), instead of waiting out `timeout`. Reads, and other cache directories, keep working. The parent's before-hook emits a `RuntimeWarning`.
  - It's only reachable through `transact()` (Phase 6b) or the private test hook.
  - **Detection is by inode.** SQLite's lock table is per inode, so a fresh `Cache` on the same directory in the child is poisoned too.
    - `begin_hold`/`end_hold` record the database's `(st_dev, st_ino)` (from `std::os::unix::fs::MetadataExt`, safe Rust) in a process-wide list, under the gate.
    - `end_fork_child` copies that list into `POISONED` and sets an `AtomicBool`.
    - Every write path does one relaxed load (false in practice) and, only if it's set, compares against the `FileId` the store took at open.
    - Grandchildren inherit the list, which is correct.
  - **Covered paths:** every write path, the spill locked fallback (skipped; section 4), the D-013 compare-and-delete (silently skipped), open-time settings and schema writes, and maintenance. The error is `Error::Unwritable`, distinct from `ForkPoisoned` (the poisoned transaction itself).
- **Two threads forking at once while each holds its own transaction.** As built in Spike 5, this deadlocks in `begin_fork`. It's fixable: a thread parked in `begin_fork` is running Python, not inside SQLite, so its held levels count as quiescent. `ForkState` records parked forkers' levels, and the first fork proceeds. The registry then poisons both databases in the child. Phase 2 implements and tests this.
- **`HeldShare` dropped on another thread.** It's `Send`, and on 3.14t the GC can finalize its holding Python object on any thread. A cross-thread drop must not leave the original thread's depth stuck at 1 or more, because that thread would then walk past every pending fork. `HeldShare` carries an `Arc<AtomicUsize>` of the owning thread's held levels. "Own depth" is the thread-local `Cell` plus that counter, read only when a fork is pending, so the hot path is unchanged. Fixed in the Phase 1 review fixes, with a unit test.
- **Changes in the Phase 1 review fixes:**
  - `Mutex` + `Condvar` were replaced with the compare-and-swap claim and sleep-polls;
  - the fork hooks register exactly once;
  - a leftover `ForkGuard` from a skipped after-hook is ended by the next before-hook;
  - pool methods take `&SharedGuard` throughout;
  - errors are classified in one place (`Error::from_sqlite`), with new `DiskFull`, `SqliteIo` and `Unwritable` variants. `errno` is always `None` for now, because rusqlite has no safe `sqlite3_system_errno`.
- **Measured after the fixes** (stress run with 8 threads and 50 forks; `fork()` median / max):
  - 3.14: 1.0 / 3.9 ms;
  - 3.14t: 2.4 / 3.6 ms;
  - 3.14t with a parallel `gc.collect()` loop: 45 / 751 ms, and still no hangs.

  The 751 ms worst case is fork waiting on stop-the-world collections. It's worth watching in Phase 4.

## 9. Prefix scan

`iterkeys(*, prefix: str | None = None, reverse: bool = False)` is a keyset-paged range scan:

- `key_type = 1 AND key >= ?prefix AND key < ?upper AND key > ?cursor ORDER BY key LIMIT 512`;
- reversed: `key < ?cursor ... ORDER BY key DESC`.

- `?upper` is the smallest string greater than every string with the prefix: increment the prefix's last code point, skipping the surrogate range (U+D800-U+DFFF jumps to U+E000). If the last code point is U+10FFFF, drop it and increment the one before, carrying. A prefix made only of U+10FFFF characters, or an empty prefix, has no upper bound, and the condition is omitted. Keys are compared in SQLite's BINARY collation over UTF-8, which orders by code point, so this bound is exact. A unit test covers each case.
- It isn't a snapshot (like diskcache's) and includes expired-but-unswept rows. Measured at 10.2 ms for 400k keys, against diskcache's 25.6 s (spike 4a; upstream bug #11).

`NullCache` and `CacheLike` get the same keyword. `delete_prefix(prefix) -> int`, chunked like `clear()`, is a candidate for Phase 6a.

## 10. Testing and gates for Phase 2

- **Conformance:** `--backend=oxide` must pass everything that isn't a documented divergence or `requires(...)` a later feature. The skip summary lists what's left.
- **Rust unit tests in core:** schema and open sequence, the spill protocol, pool generations, gate behavior, chunk bounds, and totals. Never fork in `cargo test`.
- **Hypothesis** codec round-trips, and the stubtest signature-equality test across `Cache`, `NullCache` and `CacheLike`.
- **Latency gate** (`bench/gate.py`, extended to all baseline workloads): every workload no slower than diskcache. Phase 2's real `Cache` must also stay within 10% of the Spike 2 numbers for `get_hit_small`, `get_miss` and `set_small`, and `get_hit_small_dict` must stay no slower than diskcache's pickle path (projected 1.3 µs vs 2.7 µs).
- `scripts/check.sh` must pass on all four Pythons.
- **New tests Phase 2 must add** (from the Phase 1 review):
  - a D-011 oxide test (writes made during `clear()` survive);
  - a proof of Test B (racing creators) against the oxide open sequence;
  - a free-threaded stress test that mutates a dict while another thread encodes it;
  - the detached busy-retry tests (GIL freeze, fork latency);
  - the `HeldShare` cross-thread test;
  - the D-020 registry tests (a fresh `Cache` on the same directory in the child is poisoned too, and so are grandchildren);
  - the two-forkers test;
  - write-lock tests: a fork while it's held (the child resets it), depth > 0 `try_lock`, cross-thread token drop, and same-process fairness during `clear()`;
  - an empty `BEGIN IMMEDIATE`/`COMMIT` leaves a peer's `data_version` unchanged (section 4, as a step-1 unit test);
  - the totals property test (section 3);
  - `incr` bumps `store_seq` (section 5).

## 11. Open items (carried into Phase 2)

1. Measure several concurrent writers during a `clear()`, and tune the lock-wait constants (section 7) against them.
2. Linux: the whole suite and the spike experiments. Only macOS/APFS has been measured so far.
3. Inline-vs-spill read latency between 32 and 128 KiB, to sharpen the threshold choice.
4. Phase 4: free-threaded throughput, container-encode fast paths, Rust-side datetime formatting.

## 12. Phase 2 work breakdown (for its own plan)

1. Schema, the open sequence, settings, totals and `RowWriter`, the pool (with its cap and `PooledConn<'g>`), and the two-part lock wait in core, with unit tests. This includes the empty-transaction `data_version` check.
2. The gate, generation and fork hooks promoted from Spike 5, plus the D-020 fail-fast child-write check.
3. Raw str/bytes/int get/set/add/delete/incr/touch/pop/expiry in core, plus the pyclass.
4. Codecs promoted from `spikes/codec`, the pickle opt-in and the read-side policy.
5. Spill protocol and read path.
6. Culling, expire/clear/cull with chunking, and `iterkeys(prefix=)`/`__iter__`.
7. `NullCache`, `CacheLike`, the stubs and the signature test.
8. Conformance against `--backend=oxide`, the latency gate, divergence registry updates, then the architect's end-of-phase review, commit and push.
