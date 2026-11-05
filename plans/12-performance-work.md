# Plan 12: the performance work from plan 11

Builds the ranked changes in `plans/11-read-scaling-and-incr-analysis.md`, correctness first (Michael, 2026-11-05: "work on all"). Each step is small, reviewed by `the-rust-architect`, passes `scripts/check.sh` on all four interpreters, and is committed and pushed to `main` as it lands (section 10 of plan 10). Steps that touch the fork gate, the genlock or the pool also pass `scripts/model-check.sh`, and any change to `get_with`, `pop` or the locking needs a fresh 24-hour soak on the mini before production (Michael runs it).

Order is by risk: the contained changes first, so a later, harder step can't hold back the earlier wins. Step 5 runs after step 6, and only if a measurement then still justifies it.

## Step 1: an in-place `incr`

**Why:** oxide's `incr` on a live key costs about 24 µs against stock's 17 µs, and Talk Python runs three per request (plan 11, section 3).

**What:** inside the existing `write_files` closure (so the read and the write both run under `BEGIN IMMEDIATE`, and the savepoint path inside a held transaction still applies, `store.rs:759-765`), one lookup of the row's id, mode, value, expire_time and filename, with no `PRELUDE_SQL` (its expired-row `EXISTS` sweep check is part of the cost being removed). Then:

- **Live number:** one `UPDATE cache SET mode = ?, value = ?, store_time = ?, store_seq = MAX(COALESCE((SELECT MAX(store_seq) FROM cache), 0) + 1, (SELECT next_seq FROM totals WHERE only = 1)) WHERE rowid = ? RETURNING store_seq`. The new version comes from the same expression the upsert uses (`rows.rs:104-108`), never from `totals.next_seq` alone: that's a floor raised before deletes (`rows.rs:7-11`, `raise_seq_floor` at `rows.rs:202`), not a counter, and using it could repeat an older version of the row (a stale `delete_version` would then delete the fresh value) or fall below a running `clear()`/`expire()` snapshot. `totals` isn't written. `mode` is set because an int plus a float delta is a Real (`ops.rs:125-133`). `expire_time` is kept.
- **Miss or expired:** the existing upsert path, unchanged.
- **Anything else:** `NotNumeric`, as today. (Inline numbers always have size 0 and are never spilled, `ops.rs:52`, so there's no size-change or file-backed case.)

The fast path deliberately skips `cull` and the expired-row sweep, which every `incr` runs today (`ops.rs:438`, `ops.rs:605-609`). That matches diskcache's in-place `UPDATE` but changes oxide's behavior, so it's pinned by a test.

**Done when:** the conformance suite's `incr`/`decr` cases pass against both backends, and new unit tests show:

- the version strictly increases and never repeats after a delete-then-`incr`;
- an `incr` made during a chunked `clear()` survives;
- a stale version's `delete_version` after an `incr` returns `Gone`;
- Int to Real changes the mode;
- the expiry is kept;
- the fast path runs no cull and no sweep;
- `hot_statements_are_never_reprepared` (`ops.rs:1192`) still holds.

`bench/scaling/incr_series.py` shows the gain (target: at or below stock's 17 µs on the Mac). This is a second writer of `store_seq`, so it needs a fresh soak before production.

## Step 2: the commit gap (investigation)

**Why:** for the same two WAL pages, oxide's commit costs about 2.8 µs more than stock's (plan 11, section 3).

**What:** rule out, in order: the C compiler flags the cc crate uses for the bundled SQLite (optimization level, `-fno-...` flags, LTO), and SQLite 3.54.1 against 3.53.1 (build once against 3.53.1 and rerun `incr_series.py`). Change the build only if a cause is found and the fix is a flag; a SQLite version pin needs Michael.

**Done when:** a short results note in this plan names the cause or says what was ruled out.

## Step 3: public `transact()` (roadmap Phase 6b)

**Why:** grouping a request's three counter bumps into one write transaction cuts them from 79 to 48 µs on oxide, the biggest single win found (plan 11, section 4). Stock diskcache has `transact()`; oxide only has the `_hold_write_transaction()` test hook.

**Prerequisites (blocking):**

- Bound, or explicitly accept and document, the unbounded waits the gate docs say `transact()` must bound before it ships (`gate.rs:36-41`: `HeldShare::enter` while the owner is forking or parked, or when the share was inherited), and the three waits in `DIVERGENCES.md:224`.
- Verify carry-overs 6 and 7 are fixed. Both already are: `begin_hold` uses `try_lock_nested` and fails with `WriteLockBusyInTransaction` at gate depth > 0 (`store.rs:1161-1168`, test at `store.rs:2169`), and `pop` decodes after commit (`ops.rs:345-351`).

**What:** `Cache.transact(retry=False)` as a context manager on the existing held-transaction machinery (`begin_hold`), used as `with cache.transact() as tx:`. `tx` is the transaction handle, whose methods are the cache's; on the owning thread, calls on the cache itself inside the block are in the transaction too, as in diskcache (Michael, 2026-11-05). An async form, `async with cache.transact_async() as tx:`, is deferred to `AsyncCache` (roadmap Phase 5), where waiting for the write lock never blocks the event loop (Michael, 2026-11-05).

- **Reentrancy lives in the binding.** The core refuses a second hold on one thread (`NestedTransaction`, `store.rs:1153`), so the Python `transact()` checks `held_by_this_thread()` and returns a pass-through context manager. As in diskcache, an inner block's exit doesn't commit, and an inner exception the caller catches doesn't undo the inner block's writes (no per-block savepoint).
- **Cross-cache refusal.** Inside one cache's transaction, starting one on another cache fails at once with `WriteLockBusyInTransaction`. It surfaces as a `Timeout` subclass so `retry=True` can tell it apart and never retries it (both errors map to plain `Timeout` today, `py/src/lib.rs:75`). diskcache's `Cache.transact` defaults to `retry=False`.
- **Durable behavior is already decided; pin it.** Starting a transaction raises on a durable handle (`DIVERGENCES.md:236`). A failure inside the block raises too, because durable never swallows while this thread holds a transaction on the store (`cache.rs:546-549`). A failed COMMIT raises (`close_pinned`, `store.rs` around line 1503). Pin D-020 through the public API (`DIVERGENCES.md:152`).
- **Transactions belong to a thread, not a task.** The docs say an `await` inside the block lets another coroutine on the same loop thread write inside the transaction and roll back with it, and that a fork waits, with no timeout, for every open transaction.
- **Lock order** stays write lock, then gate, then connection (`store.rs:9-16`).
- **API surface:** add `transact` to `CacheLike`, `NullCache` (a no-op context manager, including `NullCache(invalidate=)`), the stubs, and `Caches` if it forwards. Keep `_hold_write_transaction` as a thin alias until the D-012 tests move to the public API, then remove it. Divergence entries for every difference from diskcache.

**Done when:** conformance tests against diskcache for the shared semantics (commit on exit, rollback on exception, reentrancy, both nested-block rules), and unit tests for:

- fork from a non-owner thread mid-transaction, and fork from the owner (D-020);
- commit or rollback from another thread while the owner is inside an operation (`store.rs:1405-1433`);
- a GC finalizer dropping the context manager inside an operation (the `abandon` path);
- the cross-cache refusal with `retry=True`;
- a durable handle whose BEGIN times out.

`scripts/model-check.sh` passes, the user guide is updated, and a fresh soak is required.

## Step 4: Talk Python groups its counter bumps

**Why:** the measured 31 µs per request.

**What:** on the `diskcache-oxide-cache` branch of Talk Python, `rate_limit_service.check_request_async` runs its counter bumps inside one `transact()` on the `rate_limits` cache, with no `await` inside the block, since a transaction belongs to the thread and another coroutine would write inside it. A durable handle doesn't swallow failures inside a transaction, so one raise rolls back all three bumps. The existing fail-open path therefore wraps the whole `with`, and a transaction that can't begin fails open the same way. Needs a library wheel with `transact()`: Michael decides the version and the release. Until then the change waits on the branch, unpushed.

**Done when:** the app's suite passes (`ty`, ruff, pytest) against a locally built wheel, with tests that the bumps share one transaction, that a failure rolls all of them back and fails open, and that nothing in the block awaits.

## Step 5: a read path without the pool mutex (after step 6, only if measured)

**Why:** on Linux, oxide's shared-handle reads reach about 1.5M/s against plain `sqlite3`'s 2.0M on one file. The pool mutex is 7% of the waits (plan 11, section 2). Sharding (step 6) divides that contention too, and production runs one single-threaded worker, so this is built only if a measurement after step 6 still justifies it.

**What, if built:** no true thread-locals: the fork hook and close-on-last-handle (D-018) can't reach another thread's thread-locals, thread-exit destructors would have to enter the gate, and one connection per thread breaks the D-023 memory bound. Instead, a store-owned array of `max_connections` slots, each with its own mutex on its own cache line. A thread tries its "home" slot first and falls back to the pool. Fork quiescence, `close_idle_all` (`pool.rs:9-15,132`), the rule that pool locks are only taken under the gate (`pool.rs:19-22`), generations and D-018 all keep working. The fast read path's `slots.try_acquire` plus `GATE.try_enter_shared` (`store.rs:707-710`) is the fork gate: don't change it without a shuttle model.

**Done when:** `scripts/model-check.sh` passes with the slot path modelled, the fork tests pass on all four interpreters, `bench/scaling/scale_probe.py` and `conn_sweep.py` in the Linux VM show the gain, and the plan records that a fresh soak is required.

## Step 6: FanoutCache (roadmap Phase 6c)

**Why:** the only change that removes SQLite's per-file shared-memory mutex (plan 11, section 2: 3.4M vs 1.5M reads/s on Linux with four threads).

**What:** the roadmap's confirmed decisions, plus the points where diskcache's real FanoutCache differs. Each difference gets a divergence entry:

- **`transact()` within one shard** (Michael's decision): a shard-scoped `transact(key)`. diskcache's `FanoutCache.transact()` locks every shard in order (`diskcache/fanout.py:70-100`), and oxide's rule that a second hold never waits makes that impossible on `begin_hold`.
- **Failures follow the handle's `durable` setting.** diskcache's FanoutCache catches `Timeout` and returns False or the default (`fanout.py:125,158`); oxide's `durable=True` already gives that, with logging, and `durable=False` raises.
- **`size_limit` is divided by the shard count**, as diskcache does (`fanout.py:38`), with per-shard culling.
- **The shard is chosen by a fixed-seed hash of `(key_type, key bytes)`**, frozen in the format, so the non-`str` keys planned for 6a need no new format.
- **The shard count is persisted safely.** Two processes creating the directory at once must agree (temporary file plus rename, or `O_EXCL`, plus the format version stamp). An opener that passes a different `shards=` gets a `ValueError`; `shards=None` reads whatever is stored.
- **The connection budget is per shard,** summing to the handle's cap, rather than one shared slot counter, which would bring back the shared atomic sharding removes.
- **Fanned out:** `len`, `clear`, `volume` and `expire`, with a `clear` or `expire` that fails partway reporting which shards finished. `__iter__` goes shard by shard, with a divergence entry. Sorted `iterkeys` is a k-way merge that holds no connection between yields.
- **`Caches` can register one.**

It gets its own design section, reviewed by the architect, before building, since it's a new on-disk format.

**Done when:**

- conformance tests against diskcache's FanoutCache for the shared semantics;
- format tests pinning the hash and the layout;
- the fd-budget and fork tests run with N stores (`DIVERGENCES.md:130`'s fd budget becomes N times larger);
- the roadmap's fanout benchmark;
- the user guide.

## Step 7: measure again

Rerun Talk Python's benchmark (`APP/temp/cache_bench/`) at one and four threads, and `bench/scaling/` on the Mac and in the Linux VM, and update the results page (https://claude.ai/artifact/XK2UgG6FEZpsLewJbyrzNt).

## Progress

- 2026-11-05: plan written; the-rust-architect review: PASS with required changes, all applied (step 1's version source and `mode`, step 3's real prerequisites and Python-side reentrancy and retry, step 4's no-`await` rule, step 5 moved after step 6, step 6's diskcache differences).
- 2026-11-05: step 1 done. `incr` on a live number is one in-place `UPDATE` (`rows.rs` `UPDATE_NUMBER_SQL`), about 24 to 15.5-20 µs on the Mac, at or below stock's 18-20 µs in the same session. Architect: PASS after its required test fixes (a mutation that drops the version floor is now caught by two tests). `scripts/check.sh` passes on all four interpreters. Needs a fresh soak before production.
