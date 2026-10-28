# Phase 2: Core Cache (the Talk Python subset)

Parent: [00-roadmap.md](00-roadmap.md). Design: [04-design.md](04-design.md) (the contract for this phase). Follows [03-phase-1-foundation-spikes.md](03-phase-1-foundation-spikes.md).

## Goal

Replace the Spike 2 `Cache` with the real one:
- the API in design section 2;
- the format in section 3;
- the spill protocol in section 4;
- the codecs in section 5;
- expiry, culling and maintenance in section 6;
- concurrency in section 7;
- fork safety in section 8;
- the prefix scan in section 9;
- plus `NullCache` and `CacheLike`.

At the end, `--backend=oxide` passes the conformance suite (everything except documented divergences and `requires(...)` features scheduled later). The latency gate also passes at every workload, and Phase 3 (the Talk Python cutover) can begin.

Status: plan reviewed by the architect (round 1 incorporated).

## Concurrency rules (from the review; these extend 04-design.md sections 7 and 8)

1. **Lock order: write lock, then gate, then pool.**
   - At gate depth 0, the write lock is acquired with a blocking wait, interpreter released.
   - At depth > 0 (inside a held transaction, or a nested operation), it's `try_lock` only, falling back to the busy/detached path. The owning thread re-enters without waiting.
   - Never wait on the write lock while holding the gate. Otherwise this deadlocks: X holds the write lock and is blocked entering the gate because a fork is pending; T, at depth > 0, waits for the write lock; `begin_fork` waits for T.
2. **The write lock is built from atomics, not `std::sync::Mutex`, and tagged with `gate::generation()`.**
   - A writer can hold it outside the gate: between acquiring it and entering the gate, or while sleeping in a detached retry. So a child can inherit it held with no owner. A stale generation resets it, the same pattern as `reset_if_stale`.
   - The D-020 poison check runs *before* any write-lock wait, so a poisoned child fails fast instead of waiting out `timeout`.
3. **The write lock is a `Send` token (`WriteToken`).**
   - It is ready for `transact()` in Phase 6b. A cross-thread drop is safe, as with `HeldShare`.
   - `RowWriter` can only be built from a write transaction that consumes a `WriteToken`. So every `BEGIN IMMEDIATE` goes through the lock by construction: writes, the spill fallback, `incr`, open-time schema and settings writes, and maintenance chunks.
   - The D-013 compare-and-delete uses `try_lock` only, matching its zero busy timeout.
4. **Fairness inside one process.**
   - The write lock keeps a `waiters` count. Between chunks, `clear()`/`expire()`/`cull()` yield until a waiting writer has taken the lock, or about 2 ms passes. Otherwise a sleep-polling waiter loses every 100 µs gap to the maintenance loop itself, and the 20/20 fairness target fails.
   - **One deadline per operation.** The write-lock wait, the pool wait and the busy retry share the operation's `timeout`; they don't each get a separate one.
5. **The `Store` registry.**
   - **Ownership:** handles hold `Arc<Store>`, and the registry holds `Weak`. Dead entries are pruned on insert, never in `Drop`, which can run on any thread. The hot path borrows `&Store` from the frozen pyclass and never clones the `Arc` per operation (contended refcounts are the 3.14t problem).
   - **Registry mutex:** taken only under the gate, and quiesced in `begin_fork` like `pool::REGISTRY`. The open sequence never runs while holding it: open outside the lock, insert if absent, and drop the duplicate if another thread won.
   - **Inode reuse:** a `Store` keeps one fd to its database file open for its whole life, so its `(st_dev, st_ino)` can't be reused by a recreated file in the same process.
   - **Fork:** the inherited `Store` is reset in place by generation (pool, write lock, cached state), so one file still has one `Store` in the child.
   - **`close()`:** sets a `closed` flag on that handle, and the `Store` keeps an open-handle count. The pool drains when the last handle closes; D-018's wording becomes "the last handle".
   - **`max_connections`:** the pool cap is the largest value requested by any live handle on that `Store`. D-017 and design section 7 are updated to say "per database file (shared by its handles)".
   - **Limits:** `size_limit` and `cull_limit` live in the `totals` row (format v1 hasn't shipped, so it can still change) and are read inside each write transaction. Culling therefore never uses a value another process has since changed. The cached copies only serve the attribute getters.

## Ground rules

- **Behavior.** The conformance suite is the behavior contract, and 04-design.md is the implementation contract. When they disagree, the suite wins, the design gets fixed, and the architect hears about it at the end-of-phase review.
- **Steps.** Each step ends green: `cargo fmt/clippy/test`, `scripts/check.sh` (the diskcache suite on all four Pythons), and the step's own oxide tests. Steps are done one at a time. Steps that touch independent modules may run in parallel through subagents, with clear file ownership.
- **Spike code.** The spike `Cache` stays usable until step 4 replaces it, so the fork tests keep running against oxide throughout.
- **Commits.** No mid-phase commits. The phase ends with the architect's review, then one commit and push (Michael's standing rule).

## Steps

### Step 1a: schema, open sequence, RowWriter, totals
### Step 1b: pool cap, lock waits, write lock, Store registry

Step 1 is split in two (review). 1a covers `schema.rs`, `open.rs` and `RowWriter` with the totals. 1b covers the `max_connections` cap, `lockwait.rs`, the write lock and the `Store` registry, all following the concurrency rules above. Step 1b owns any generation-reset hooks it adds to `gate.rs`. Step 2 runs after 1b, so the two never edit `gate.rs` at the same time.

New modules in `crates/diskcache-oxide-core/src/`:

- **`schema.rs`:** the format-version-1 DDL (design section 3: `AUTOINCREMENT` id, `key_type`, `mode`, `store_seq`, the `totals` row, partial indexes).
- **`open.rs`:** the open sequence that writes nothing when nothing changed:
  - WAL is set before the schema transaction;
  - `user_version` is re-checked under `BEGIN IMMEDIATE`;
  - `FormatMismatch` when the version isn't ours;
  - settings use `None` sentinels;
  - directories are created 0700.
- **`store.rs`:**
  - `Store`, the process-wide object shared by every `Cache` handle on the same database file, found through a registry keyed by `(st_dev, st_ino)`. It owns the pool, the in-process write lock and the cached settings. Extra `Cache` handles are therefore cheap.
  - `RowWriter`, the single choke point for every row change. It keeps `totals` correct and assigns `store_seq` from `totals.next_seq`.
- **`pool.rs`** (extended):
  - the `max_connections` cap;
  - `PooledConn<'g>`, which checks itself in on drop;
  - waits at depth 0 happen before entering the gate (polled, interpreter released); at depth > 0 a nested operation opens over the cap.
- **`lockwait.rs`:**
  - the busy handler, with a per-connection `Arc<AtomicU64>` deadline, polling 10 µs doubling to 100 µs for about 2 ms while attached;
  - the detached retry loop (roll back, check in, leave the gate, sleep 100 µs rising to 1 ms, retry until `timeout`);
  - **the in-process write lock per `Store`:** same-process writers queue on it with the interpreter released before `BEGIN IMMEDIATE`, so only other processes meet SQLite's busy handler (design section 7, "Actions");
  - one set of constants in `core::sqlite`.

Unit tests:
- the open sequence writes nothing on a reopen with the same settings (peer `data_version`);
- an empty `BEGIN IMMEDIATE`/`COMMIT` leaves a peer's `data_version` unchanged (design section 4);
- racing creators of a new database (threads here; processes in pytest);
- a property test that runs random operations, then checks `totals` against `COUNT`/`SUM`;
- `store_seq` never repeats after deleting the newest row;
- pool cap and checkin-on-drop;
- the write lock serializes same-process writers;
- a lock-wait deadline produces `Timeout`.

### Step 2: gate and fork, promoted and completed

Extend `gate.rs` (kept from Spike 5) with:
- **the D-020 registry:** `begin_hold`/`end_hold` record `(st_dev, st_ino)`; `end_fork_child` copies the list into `POISONED`; write paths do a relaxed load, then compare with the store's `FileId`, and fail with `Error::Unwritable`;
- **the two-forkers fix:** the held levels of threads parked in `begin_fork` count as quiescent.

Unit tests: the registry lists and the two-forkers case. Fork tests stay in pytest.

### Step 3: raw operations in core

On `Store` and `RowWriter`:
- `get` (including `expire_time`), `set`, `add`, `delete`, `touch`, `pop`;
- `incr`/`decr`: read, `checked_add`, then write inside one `BEGIN IMMEDIATE`. Modes 6 and 7. They bump `store_seq`, raise `OverflowError` past 64 bits, and raise `TypeError` on non-numeric values;
- expiry semantics exactly as pinned in design section 6;
- culling with the shared `cull_limit` budget;
- `clock=` called once per operation, before a connection is taken.

The core API takes and returns `(mode, bytes)` or a value-file handle and never sees Python.

Unit tests: each pinned expiry, `incr` and culling rule from the conformance catalog that can be expressed without Python.

### Step 4: the pyclass and codecs

- **`crates/diskcache-oxide-py/src/cache.rs`:** the real `#[pyclass(frozen)] Cache`, with the design section 2 signature, validation (D-005, D-014, D-004), attributes, `enabled`, `__bool__` (D-009), the mapping protocol and the context manager. The spike `Cache` and `spike.rs` in both crates are deleted.
- **Codecs:** `codec/{common,json,msgpack}.rs`, promoted from `spikes/codec`:
  - `try_borrow_mut` on `KEYS`;
  - a size-based output buffer;
  - exact types only;
  - modes 3/4/5;
  - pickle only with `pickle=True`;
  - the read-side `UnsafeValueError`;
  - corrupt rows raise `CorruptValueError`.
- **Errors:** created with `create_exception!` under `diskcache_oxide`, and mapped from core in one place.
- **Python side:** `_core.pyi` updated, plus `python/diskcache_oxide/__init__.py`.
- **Tests:**
  - the Hypothesis round-trip tests from `spikes/codec/tests` move to `tests/unit/test_codec.py`;
  - the free-threaded stress test (one thread mutates a dict while another encodes it: either a clean round trip or a `RuntimeError`/`TypeError`, never a crash or a corrupt row).

After this step, the conformance suite runs against the real oxide `Cache`.
- **`_hold_write_transaction()` exists from this step**, so the fork tests keep running once the spike `Cache` is deleted.
- **`scripts/check.sh` gains a `--backend=oxide` run** against a committed expected-results file (`tests/oxide_expected.txt`: node ids still expected to fail or skip, each with the step that will fix it). The file may only shrink. A node that newly fails, or one that passes while still listed, fails the check.
- **Step 9's mechanical check:** a `FEATURE_PHASES` map in `backends.py` gives every `requires()` feature its later phase, and a `requires()` feature without a phase fails collection. In the oxide session, any skip whose reason isn't a divergence, a not-yet feature or a backend-only marker (for example a stray `skipif` or `importorskip`) fails the run.

### Step 5: spill protocol

`spill.rs`:
- the 64 KiB threshold;
- `aa/bb/<32 hex>.tmp` → `.val`, with the rename under the write lock;
- unlink after commit;
- the length check against `size` on read;
- 8 optimistic attempts, then the locked fallback (skipped in a D-020 child);
- the compare-and-delete through `RowWriter`, with a zero busy timeout.

Value-file I/O runs with the interpreter released. The adapter's `spill_threshold` = 65536 is already set.

Tests: Test A (D-016), Test I (SIGKILL), the D-013 tests and the spilled-value locking tests, all against oxide.

### Step 6: maintenance and iteration

- `expire()`, `clear()` and `cull()` in chunks of 100, bounded by a `store_seq` snapshot, with unlinks after each chunk's commit and a 100 µs pause (interpreter released) between chunks.
- `__iter__` (insertion order by `id`) and `iterkeys(*, prefix=None, reverse=False)` (keyset-paged, 512 per chunk, `?upper` per design section 9), with the connection returned between chunks.
- `volume()`, `__len__` from `totals`, `close()` (D-018).

Tests:
- a D-011 oxide test (writes made during `clear()` survive);
- the fairness target: a writer with `timeout=1.0` gets in during a 50k-row `clear()` 20/20 times;
- the `?upper` unit tests;
- a prefix-scan benchmark entry.

### Step 7: NullCache and CacheLike

- **`python/diskcache_oxide/null.py`:** `NullCache`, following the instant-expiry rule from the roadmap, with `invalidate=` forwarding `delete`, `clear`, `iterkeys` and `pop` (as a delete). It accepts and ignores `Cache`'s constructor arguments. It's falsy and `enabled` is False.
- **`python/diskcache_oxide/protocol.py`:** `CacheLike` (a `Protocol`).
- **Tests:**
  - `tests/unit/test_null_cache.py`: every method's result follows from the rule, and forwarding works;
  - a signature-equality test across `Cache`, `NullCache` and `CacheLike`, comparing `inspect.signature` on the Python side and the `.pyi` for `Cache`;
  - mypy and pyright both see `Cache` and `NullCache` as satisfying `CacheLike`.

### Step 8: benchmarks and the gate

- **`bench/baseline.py`:** the threaded workload becomes the share-nothing loop at **99:1** (the headline) plus a **1,000:1 Talk Python profile**, at 1, 2, 4 and 8 threads. Add `get_hit_small_dict` and a prefix-scan workload (`iterkeys(prefix=)` for oxide, iterkeys plus a filter for diskcache, reported as separate lines).
- **Re-baseline** diskcache on AC power, on 3.14 and 3.14t.
- **`bench/gate.py`:** extended to every baseline workload.
- **New workload:** a two-process 99:1 benchmark (two processes × 1, 2 and 4 threads on one directory). The write lock changes cross-process behavior, and nothing else measures it.
- **Pass criteria** (medians of the interleaved rounds, tolerance 1.05× everywhere):
  - every workload is no slower than diskcache;
  - `get_hit_small` and `get_miss` stay within 10% of the Spike 2 numbers (≤ 0.88 / 0.80 µs);
  - `set_small` gets a realistic budget, ≤ 0.6× diskcache's `set_small` (about 15 µs). The real `set` does more than the spike did: the totals update, a cull check, the old-filename read and the write lock;
  - `get_hit_small_dict` is no slower than diskcache's pickle path;
  - threaded throughput at 99:1 and 1,000:1 is no lower than diskcache's at every thread count, on both builds, and so is the two-process benchmark.

### Step 9: finish

- **Full suite against `--backend=oxide`** on all four Pythons:
  - every test that remains skipped or xfailed must map to a `DIVERGENCES.md` entry or a `requires(...)` feature with a named later phase;
  - add oxide to `scripts/check.sh` (a run with `--backend=oxide`);
  - fix the `--backend=oxide` adapter (`OXIDE_SUPPORTED_FEATURES` gains `cull_limit`, `fd_budget` reads `max_connections`, `sqlite_info()`).
- **The new tests from design section 10:**
  - D-011;
  - Test B against the oxide open sequence;
  - the encode-while-mutating stress test;
  - the detached busy-retry GIL-freeze and fork-latency tests;
  - the `HeldShare` cross-thread test;
  - the D-020 registry tests (including a fresh `Cache` on the same directory in the child, and grandchildren);
  - two forkers at once;
  - `incr` bumps `store_seq`.
- **Fork tests:** 20/20 on 3.14 and 3.14t.
- **Stress tests** (`-m slow`) green.
- **Cleanup:** delete `spikes/` (it stays in git history); update the README with a short "status / not yet usable" note.
- **Review:** the architect's end-of-phase review, fixes, then commit and push.

## Parallelism

- **Steps 1a and 1b are mine:** they define `Store`, `RowWriter`, `WriteToken` and the pool API everything else uses.
- **The codec half of step 4** (`codec/*.rs`, which depends only on PyO3) can run as a parallel subagent from the start.
- **Step 2** (`gate.rs`) runs after 1b, which may add generation-reset hooks to the same file.
- **Steps 3, 5 and 6** touch `store.rs` and run in sequence.
- **Step 7** is pure Python and can run in parallel with steps 5 and 6.
- **Step 8's benchmark rewrite** can happen early, in parallel, because it only touches `bench/`.

## Exit criteria

- `--backend=oxide` conformance on 3.14, 3.14t, 3.15 and 3.15t: no failures, and every skip or xfail is accounted for.
- The latency gate passes on 3.14 and 3.14t, with the numbers recorded in this plan. Since the architect's end-of-phase review, `bench/gate.py` also enforces `get_hit_small` ≤ 0.55× and `set_small` ≤ 0.85× diskcache (`TARGETS`). Those replace the original absolute targets (`set` ≤ 0.6×, `get` ≤ 0.88 µs): the `set` floor is SQLite's own upsert cost, and absolute µs don't carry over between machines.
- `scripts/check.sh` passes (diskcache and oxide).
- The fork tests pass 20/20, and the stress tests pass.
- The spike code is gone.
- The full conformance suite (diskcache and oxide) and the stress tests pass on Linux too: the latest Ubuntu LTS in OrbStack Docker. The gate benchmarks also run there, and the results are recorded.
- The architect's end-of-phase review is done, with its fixes applied.

## Out of scope

- The Talk Python changes (Phase 3) and any PyPI release.
- Phase 6 features: tags, other eviction policies, `memoize`, `transact`, `check()`, `read=`, FanoutCache, non-`str` keys.
- Performance work beyond the gate (Phase 4), except the in-process write lock and the shared `Store`, which the design moved into this phase.

## Progress

- **Step 1a, done (2026-10-28):**
  - `schema.rs` (format v1, with `size_limit`/`cull_limit` in `totals`);
  - `open.rs` (WAL before schema, a re-checked `user_version`, `FormatMismatch`, `None` settings sentinels, writing only on a mismatch, a 0700 directory);
  - `rows.rs` (`RowWriter`, with `store_seq` from `totals.next_seq` and an `AUTOINCREMENT` id).
  - Tests cover: no write on an unchanged reopen (peer `data_version`), an empty `BEGIN IMMEDIATE`/`COMMIT` leaving the peer's `data_version` alone, racing creators, refusing a newer format, `store_seq` and `id` never reused, the compare-and-delete, and a totals property test (3,000 random operations).
- **Step 1b, done (2026-10-28):**
  - `genlock.rs`: the in-process `WriteLock` (a `Send` `WriteToken`, a waiter count) and `Slots` (the `max_connections` cap), each a single `AtomicU64` packing generation and state, so a forked child reads an inherited hold or count as free with no reset race.
  - `lockwait.rs`: the busy handler, which only waits inside a thread-local `AttachedWindow` (about 2 ms; rusqlite's handler is a plain `fn`, and SQLite runs it on the calling thread); `retry()` for detached backoff; one set of constants.
  - `store.rs`: the `Store` registry keyed by `(st_dev, st_ino)`, with a pinned fd, `Weak` entries pruned on insert, and the mutex taken only under the gate and quiesced at fork. It provides `read()`/`write()` with the lock order write lock → gate → pool, the connection cap (skipped at depth > 0), handle counting with the pool drained on the last close, and explicit-only settings on join.
  - `gate::run_blocking`.
  - The workspace `rust-version` now matches the pinned toolchain, 1.99 (Michael: use the very latest Rust). That allows `AtomicU64::try_update` (`fetch_update` is deprecated in 1.99).
  - 52 core tests, stable across 10 runs; clippy and fmt clean.
- **Step 2, done (2026-10-28):**
  - `poison.rs` is the D-020 registry: a lock-free list of the inodes currently held, copied into `POISONED` in the child, with `check_writable` called first in `Store::write`.
  - Two threads that both fork while holding transactions no longer deadlock: a parked forker's levels count as quiescent, and a per-gate epoch keeps the counts right in the child.
  - The two-forkers script passed 200 rounds on each of the four Pythons. 70 core tests.
- **Step 3, done (2026-10-28):** `ops.rs` adds get/contains/set/add/delete/pop/touch/incr on `Store`, with the shared cull budget.
  - Sweeping uses `expire_time < now` (strict), while reads treat a row as live only when `expire_time > now`. This is the only combination consistent with two pinned behaviors: `set(expire=0)` leaves a row that `len` counts, and culling runs after the insert (D-005's diskcache side).
- **Step 7, done ahead of order:** `NullCache` (`python/diskcache_oxide/null.py`) and `CacheLike` (`protocol.py`), with 9 unit tests. The signature-equality test waits for step 4.
- **Toolchain:** Rust 1.99.1, with `rust-version` = 1.99 (Michael: "we can use the very latest rust").
- **Linux harness, done:** `docker/linux-dev.Dockerfile` (Ubuntu 26.04 LTS aarch64, as the `ubuntu` user, with tzdata), `docker/in-container-check.sh` and `scripts/linux-check.sh` (`--slow`, `--bench`, `--python`, `--exec`).
  - diskcache suite: 631 passed, 7 xfailed, with no Linux-only failures. The community backend is green, and all 11 upstream repros reproduce on Linux.
  - Gate on Linux: GO on both builds. `get` hit 0.69 µs vs diskcache's 1.88 µs on 3.14. At 99:1, oxide holds about 910-940k ops/s at 1-8 threads, while diskcache collapses 374k → 46k.
  - On 3.14t, oxide at 1,000:1 reaches 1.33M ops/s at 8 threads, but that's only 1.2× its single-thread number; this is the Phase 4 target.
- **Harness fix:** the spill and overwrite race helpers now always signal their readers and reap leftover processes (`_reaped`). A dying writer used to leave the readers spinning and hang pytest.
- **Steps 4-6, done (2026-10-28):** the real `Cache` pyclass (`cache.rs`), with the promoted codecs, held transactions (`Store::begin_hold`, used by `_hold_write_transaction()`), value files (`spill.rs`: 64 KiB threshold, 8 optimistic reads then a locked fallback, compare-and-delete through `write_nowait`), and maintenance and iteration (`maintenance.rs`: chunked `expire`/`clear`/`cull` bounded by a seq snapshot, with yields to waiting writers; keyset `iterkeys(prefix=, reverse=)` and `__iter__`). The spike modules are deleted. `NullCache`/`CacheLike` pass the signature-equality and strict-mypy tests.
  - **Conformance against `--backend=oxide`:** 315 passed, 0 skipped, 0 failed, 26 xfailed (documented divergences), on 3.14, 3.14t, 3.15 and 3.15t. `scripts/check.sh` now runs the oxide suite and `mypy --strict`.
  - **Bugs found and fixed while wiring up:**
    - `incr(default=None)` was indistinguishable from leaving `default` out (PyO3 maps both to `None`); fixed with custom argument types.
    - `volume()` double-counted inline values, and then counted SQLite freelist pages, so culling never converged. `size` now counts only value-file bytes, and volume uses the pages in use.
    - The pinned database fd stayed open after the last `close()`; the store now gives it up and retires from the registry.
    - `Cache.__exit__`'s signature didn't match `CacheLike` (caught by the signature test).
- **Step 8 performance work (2026-10-28).** The first gate run failed `set_small` at 31.4 µs (diskcache: 26.3). Profiling found three things:
  1. **Extra WAL pages.** Every write bumped `totals.next_seq`: 5.1 WAL frames per set against diskcache's 4.1. Now `store_seq = max(MAX(store_seq) + 1, floor)`, with the floor raised only on deletes, so `totals` is written only when the count or size changes. That's 4.1 frames, the same as diskcache, and D-011's never-repeat guarantee still holds.
  2. **SQL statements recompiled on every call.** rusqlite's statement cache calls `sqlite3_clear_bindings` on every reuse. SQLite expires any statement whose plan looked at a bound parameter (our `expire_time` comparisons, through the partial `cache_expire` index) when its bindings are cleared, so the prelude and `get`'s lookup were recompiled on every call (`sqlite3Reprepare`, about 30% of a set). Fixed with `SQLITE_DBCONFIG_ENABLE_QPSG` on every connection. A regression test (`ops::reprepare_guard`) asserts zero recompiles. STAT4 is also built out (`.cargo/config.toml`), since it never helps without `ANALYZE`.
  3. **The `pragma_*` table-valued functions** re-prepare their PRAGMA internally; they're replaced by cached plain PRAGMAs, and `freelist_count` is only read near the limit. A single "prelude" statement reads the old row and totals, and the expiry probe runs before the sweep. `BEGIN`/`COMMIT` are cached.

  Reads got a contention-free fast path: one attempt with no deadline or busy window, falling back to the retry loop only on a lock.
- **Gate after the fixes (AC power, 4 interleaved rounds):** GO on both builds.

  | Workload | 3.14 diskcache / oxide | 3.14t diskcache / oxide |
  |---|---|---|
  | `get_hit_small` p50 | 1.88 / **0.90 µs** (0.48×) | 1.70 / **0.90 µs** (0.53×) |
  | `get_miss` | 1.48 / **0.79** | 1.40 / **0.79** |
  | `get_hit_small_dict` | 2.65 / **1.32** | 2.49 / **1.38** |
  | `set_small` | 25.8 / **20.9 µs** (0.81×) | 25.5 / **21.3 µs** (0.83×) |
  | 99:1, 1/2/4/8 threads (kops/s) | dc 404/241/80/20; **oxide 709/699/688/701** | dc 437/386/450/477; **oxide 719/584/710/592** |
  | 1,000:1, 1/2/4/8 threads | dc 448/212/71/19; **oxide 833/842/835/840** | dc 481/403/534/395; **oxide 838/602/849/736** |

  Two of the plan's stricter criteria are not met, and both go to the architect's end-of-phase review:
  - `set_small` ≤ 0.6× diskcache: measured 0.81-0.83×. A `set` writes the same 4.1 WAL pages as diskcache, and a bare SQLite upsert transaction is about 14 µs on this machine, so 0.6× (about 15 µs) would need fewer index pages per write than diskcache. Proposal: replace it with "≤ 0.9× diskcache".
  - `get_hit_small` within 10% of the Spike 2 number (≤ 0.88 µs): measured 0.89-0.90 µs. Profiling shows about 60% of a `get` is SQLite's `-shm` read-lock `fcntl`, which isn't our code (Phase 4's top target); our own overhead is a few percent.
- **Step 9, done (2026-10-28):**
  - **Design section 10 tests.** Most were already covered by earlier steps: Test B (`test_many_openers_racing_to_create_a_new_directory` on oxide), the encode-while-mutating stress test (`tests/unit/test_codec.py`), `HeldShare` across threads and two forkers (`gate.rs`), D-020 grandchildren (`poison.rs`), cross-thread token drop and the fork-reset of the write lock (`genlock.rs`), `incr` bumps `store_seq` (`ops.rs`), the empty-transaction `data_version` check and the totals property test. New in step 9:
    - `tests/unit/test_concurrency.py`, with scenarios in `tests/unit/fork_scenarios.py`, each run in a fresh process:
      - D-020 end to end: inherited transaction, a fresh `Cache` in the child, grandchildren, other directories, and the parent's `RuntimeWarning`;
      - fork while another thread waits on another process's lock: fork latency, and the child treats the inherited write lock as free;
      - two threads forking at once under load;
      - clear fairness plus D-011 in one process: writes with `timeout=1.0` during a 50k-row `clear()` all land and survive;
      - the detached busy retry never stalls other threads (no GIL freeze).
    - `store::a_write_inside_the_gate_never_waits_for_the_write_lock` (the depth > 0 `try_lock` rule).
    - Fork tests and clear fairness pass 20/20 on 3.14 and 3.14t.
  - **Harness:** `backends.FEATURE_PHASES` maps each optional feature to the phase it lands in; `requires(...)` must name one. An oxide run fails on any skip that a marker didn't add, so "0 skipped" can be trusted.
  - **Memory bound (Michael, mid-step):** SQLite memory is now capped per cache directory. `Cache(..., sqlite_cache_size=pages)` (diskcache's name and unit) sets each connection's page cache; the default is 256 pages (1 MiB), so with 8 connections a directory's page caches stay under 8 MiB. As with `max_connections`, the largest value any handle asks for wins (D-023).
    - Measured (uniform random gets over a 145 MB database, the worst case): 2000 pages 2.61 µs, 512 2.74, **256 2.88**, 128 3.14. Hot and skewed gets, sets and the threaded mixes don't change at any size. diskcache measures 3.53 µs on the same workload, with 32 MiB of cache plus mmap.
    - The new `get_random_wide` workload in `bench/baseline.py` and `bench/gate.py` keeps it honest.
    - `tests/unit/test_memory.py` checks SQLite's own high-water mark (`_core._sqlite_memory`) against `max_connections` × (pages × 5.25 KiB + 512 KiB) under a 32-thread storm, and checks that a larger setting really raises it.
    - **Two builds (Michael):** SQLite's memory statistics take a process-wide mutex on every allocation, so the shipped build turns them off (`SQLITE_DEFAULT_MEMSTATUS=0` in `.cargo/config.toml`). An A/B over 4 interleaved rounds showed no single-thread change and 2-10% more threaded throughput (3.14t 1,000:1 at 8 threads: +10%; 3.14 at 2-8 threads: +3-7%). Performance numbers come from the shipped build. `scripts/check.sh` (and the Linux container) also builds `.venv-mem`: 3.14t with the statistics on, non-editable, with its own target dir. It runs `test_memory.py` (which fails there rather than skip, via `DCO_REQUIRE_MEMSTATUS=1`) and the heavy-load report `bench/memory.py`, whose throughput column is informational only. uv's cache keys now include `.cargo/config.toml` and `LIBSQLITE3_FLAGS`.
    - **Memory under load** (`bench/memory.py` on `.venv-mem`, 50k keys, 99:1 uniform random, defaults): SQLite's high-water mark was 1.55 MiB at 1 thread, 8.01 MiB at 8 and 9.68 MiB at 32, falling back to 1-3 MiB once the load stopped. So under real concurrency it does reach the page-cache budget (8 × 1 MiB) plus per-connection overhead. It stays inside the tested bound (8 × (256 pages × 5.25 KiB + 512 KiB) ≈ 14.5 MiB), and it doesn't grow with the thread count beyond `max_connections`.
  - **Linux harness gap fixed:** `docker/in-container-check.sh` never ran the conformance suite with `--backend=oxide` (nor `mypy --strict`), so earlier Linux runs covered only the diskcache oracle and the unit tests. It now mirrors `scripts/check.sh`.
  - **Cleanup:** `spikes/` deleted (it's in the Phase 1 commit). The README has a pre-release status note.
- **Gate on the shipped build (macOS, AC power, 4 interleaved rounds, 2026-10-28):** 256-page cache, memory statistics off. GO on both builds.

  | Workload | 3.14 diskcache / oxide | 3.14t diskcache / oxide |
  |---|---|---|
  | `get_hit_small` p50 | 1.90 / **0.90 µs** (0.48×) | 1.72 / **0.90 µs** (0.52×) |
  | `get_miss` | 1.48 / **0.79** | 1.41 / **0.79** |
  | `get_hit_small_dict` | 2.63 / **1.32** | 2.51 / **1.39** |
  | `set_small` | 26.3 / **19.5 µs** (0.74×) | 25.4 / **19.5 µs** (0.77×) |
  | `get_random_wide` | 3.49 / **2.57 µs** (0.74×) | 3.17 / **2.64 µs** (0.83×) |
  | 99:1, 1/2/4/8 threads (kops/s) | dc 395/245/78/20; **oxide 683/680/678/677** | dc 437/397/449/484; **oxide 687/619/732/601** |
  | 1,000:1, 1/2/4/8 threads | dc 437/215/74/20; **oxide 773/764/761/763** | dc 493/409/576/416; **oxide 753/583/826/750** |

  The memory cap costs nothing visible here. The two missed criteria are unchanged (`set` 0.74-0.77× against the 0.6× target; `get` hit 0.90 µs against ≤ 0.88). On 3.14t, oxide still doesn't scale with threads (8 threads ≈ 1 thread), which is the Phase 4 target.
- **Linux run, and a write-lock starvation bug (2026-10-28).** The first full Linux run of the oxide suite (the container script had been skipping it, see above) found:
  - **A clippy lint that only fires on Linux** (`drop` of a `Mutex`, which has no destructor there); fixed in `spill.rs`.
  - **Starvation in the in-process write lock**, failing 20/20 on 3.14t and 3.15t. A thread in a tight `set()` loop re-took the freed lock every time, so a waiting `clear()` (which steps aside between chunks) lost every race until its 1 s timeout. macOS's scheduler happened to hide it; the rewritten unit test reproduces it there too.
  - **Fix, in three tunings** (Linux 3.14t, 99:1 at 8 threads / `incr` write storm / `clear()` under a steady writer):
    1. A starvation mode like Go's `sync.Mutex`: a waiter that has waited too long counts itself as starving, and while any waiter is starving, fresh callers can't barge in.
    2. At 1 ms (Go's value) it collapsed: 175k ops/s (NO-GO), 6.7 s. Our waiters find a freed lock by polling, so each fair hand-off leaves the lock idle, and steady load kept the lock in starvation mode.
    3. At 20 ms: 741k, 0.71 s, but `clear()` paid 20 ms per chunk (10.3 s).
    4. Finally, a maintenance loop that just stepped aside re-takes the lock with priority (`WriteLock::lock_until_with_priority`): **720k, 0.83 s, 4.7 s**. The Linux 3.14t gate is GO (1,000:1 at 8 threads: 1.41M ops/s, 2.26× diskcache), and clear fairness passes 20/20. Mostly-read workloads never touch the starvation path; an uncontended write pays one extra atomic load.
  - **Regression test:** `genlock::a_waiter_is_not_starved_by_a_thread_that_keeps_retaking_the_lock`. With the fix disabled it starves until its deadline on both macOS and Linux. Its first version passed without the fix (its hammer barely held the lock, and only the first acquisition was really contested).
  - **For Phase 2.5/4:** replace polling with real parking (a fork-safe wait with timeouts, waking one waiter on release). That removes the idle gap at each hand-off, so fairness costs nothing. `atomic-wait` doesn't fit: it has no timeout.
- **Architect's end-of-phase review (2026-10-28).** Verdict: Phase 2 can close once the one must-fix item is in. It found no correctness bug in the write-lock starvation fix: exclusion rests only on the CAS on `word`, the race between the starving check and the CAS is benign, the counters are fork-safe, and nothing deadlocks.
  - **Must-fix, done:** a huge `timeout` panicked across the FFI. `Duration::from_secs_f64` panics above about 1.8e19 s, and `Instant + Duration` on overflow; the panic surfaces as `PanicException`, which slips past `except Exception`. The py edge now clamps to `lockwait::MAX_TIMEOUT` (100 years), and the core deadlines use `lockwait::deadline_after` (overflow-proof). Tests: `lockwait::a_huge_timeout_makes_a_far_deadline_not_a_panic` and `test_a_huge_timeout_works_instead_of_panicking` (timeouts 1e10 to 1e300).
  - **Suggested tests, added:**
    - `genlock::inherited_starving_and_waiter_counts_read_as_zero` (fork safety of the new counter);
    - `store::a_write_inside_the_gate_never_waits_for_a_starving_writer` (pins the depth > 0 no-wait rule when `try_lock` refuses).
  - **Rulings on the missed criteria:**
    - `set` ≤ 0.6× is below SQLite's floor; replaced by ≤ 0.85×, not the proposed 0.9×, which with the 1.05 tolerance couldn't catch a 15% regression.
    - `get` 0.90 µs passes the plan's own 1.05× tolerance.
    - Both are now enforced as ratios in `bench/gate.py`.
    - Our overhead over a bare SQLite upsert (about 5.5 µs) becomes a Phase 4 metric.
  - **Small items done now:**
    - the smoke test in both check scripts asserts the shipped SQLite flags (no `ENABLE_STAT4`, `DEFAULT_MEMSTATUS=0`), so a wheel built without `.cargo/config.toml` can't quietly lose about 30% of `set`;
    - the instrumented build's flags are derived from `.cargo/config.toml`;
    - `sqlite_cache_size` is capped at `i32::MAX` (SQLite reads it as a 32-bit int);
    - D-023 says "about" the cap, since held transactions and nested operations can open a connection beyond it.
  - **Carried into Phase 2.5:** nine concurrency items, listed in the roadmap's Phase 2.5 section:
    1. waiter counters leak on unwind;
    2. the interpreter re-attaches on already-detached threads;
    3. non-starving waiters keep polling;
    4. depth > 0 barging;
    5. `cull()` doesn't yield;
    6. `begin_hold` waits at depth > 0;
    7. `pop` decodes inside its write transaction;
    8. the store can split on close/rejoin;
    9. `max_idle` stays at the first handle's cap.
  - **Later:**
    - make the core modules and `RowWriter::new` `pub(crate)`, restoring rule 3's type-level guarantee;
    - a `pytest_collectreport` hook for module-level skips;
    - starve after `min(20 ms, timeout / 4)`;
    - skip the maintenance yield inside one's own held transaction;
    - optionally put `_sqlite_memory` behind a cargo feature.
- **Final verification (2026-10-28), after the review fixes:**
  - macOS `scripts/check.sh --slow`: green on all four Pythons, plus the memory build.
  - Linux `scripts/linux-check.sh --slow --bench`: green on all four Pythons (oxide: 315 passed and 26 xfailed everywhere), plus the memory build. The Linux gate is GO on both builds:

  | Workload (oxide, ratio to diskcache) | 3.14 | 3.14t |
  |---|---|---|
  | `get_hit_small` (target ≤ 0.55×) | 0.76 µs (0.42×) | 0.75 µs (0.46×) |
  | `get_miss` | 0.65 (0.45×) | 0.62 (0.46×) |
  | `get_hit_small_dict` | 1.18 (0.47×) | 1.26 (0.54×) |
  | `set_small` (target ≤ 0.85×) | 22.6 µs (0.77×) | 24.0 µs (0.82×) |
  | `get_random_wide` | 2.21 (0.65×) | 2.41 (0.79×) |
  | 99:1, 1/2/4/8 threads (kops/s) | 750/745/723/723 (2.0-16.5×) | 701/639/680/729 (1.7-1.8×) |
  | 1,000:1, 1/2/4/8 threads | 900/886/876/875 (2.1-18.9×) | 903/680/1,181/1,416 (1.25-2.2×) |

  `set_small` on Linux 3.14t is the closest to its target (0.82× against 0.85×), so watch it in Phase 2.5/4.
