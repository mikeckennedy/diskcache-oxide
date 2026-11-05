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

- Bound, or explicitly accept and document, the unbounded waits the gate docs say `transact()` must bound before it ships (`gate.rs:36-41`: `HeldShare::enter` while the owner is forking or parked, or when the share was inherited), and the three waits in `tests/DIVERGENCES.md:224`.
- Verify carry-overs 6 and 7 are fixed. Both already are: `begin_hold` uses `try_lock_nested` and fails with `WriteLockBusyInTransaction` at gate depth > 0 (`store.rs:1161-1168`, test at `store.rs:2169`), and `pop` decodes after commit (`ops.rs:345-351`).

**What:** `Cache.transact(retry=False)` as a context manager on the existing held-transaction machinery (`begin_hold`), used as `with cache.transact() as tx:`. `tx` is the transaction handle, whose methods are the cache's; on the owning thread, calls on the cache itself inside the block are in the transaction too, as in diskcache (Michael, 2026-11-05). An async form, `async with cache.transact_async() as tx:`, is deferred to `AsyncCache` (roadmap Phase 5), where waiting for the write lock never blocks the event loop (Michael, 2026-11-05).

- **Reentrancy lives in the binding.** The core refuses a second hold on one thread (`NestedTransaction`, `store.rs:1153`), so the Python `transact()` checks `held_by_this_thread()` and returns a pass-through context manager. As in diskcache, an inner block's exit doesn't commit, and an inner exception the caller catches doesn't undo the inner block's writes (no per-block savepoint).
- **Cross-cache refusal.** Inside one cache's transaction, starting one on another cache whose write lock another thread holds fails at once with `WriteLockBusyInTransaction` (a free lock is taken, `store.rs:1167`). It surfaces as a `Timeout` subclass so `retry=True` can tell it apart and never retries it (both errors map to plain `Timeout` today, `crates/diskcache-oxide-py/src/lib.rs:75`). diskcache's `Cache.transact` defaults to `retry=False`.
- **Durable behavior is already decided; pin it.** Starting a transaction raises on a durable handle (`tests/DIVERGENCES.md:236`). A failure inside the block raises too, because durable never swallows while this thread holds a transaction on the store (`cache.rs:546-549`). A failed COMMIT raises (`close_pinned`, `store.rs` around line 1503). Pin D-020 through the public API (`tests/DIVERGENCES.md:152`).
- **Transactions belong to a thread, not a task.** The docs say an `await` inside the block lets another coroutine on the same loop thread write inside the transaction and roll back with it, and that a fork waits, with no timeout, for every open transaction.
- **Lock order** stays write lock, then gate, then connection (`store.rs:9-16`).
- **API surface:** `CacheLike` stays without `transact` (Michael, 2026-11-05), so FanoutCache (plan 13) can still satisfy it; add a `TransactionalCacheLike(CacheLike)` protocol with `transact`, which `Cache` and `NullCache` satisfy. Add `transact` to `NullCache` (a no-op context manager, including `NullCache(invalidate=)`), and the stubs. `Caches` forwards no cache methods (its lookups return a `Cache` or a `NullCache`), so it needs only a test. Keep `_hold_write_transaction` as a thin alias until the D-012 tests move to the public API, then remove it. Divergence entries for every difference from diskcache.

**Done when:** conformance tests against diskcache for the shared semantics (commit on exit, rollback on exception, reentrancy, both nested-block rules), and unit tests for:

- fork from a non-owner thread mid-transaction, and fork from the owner (D-020);
- commit or rollback from another thread while the owner is inside an operation (`store.rs:1405-1433`);
- a GC finalizer dropping the context manager inside an operation (the `abandon` path);
- the cross-cache refusal with `retry=True`;
- a durable handle whose BEGIN times out.

`scripts/model-check.sh` passes, the user guide is updated, and a fresh soak is required.

### Design

Reviewed by a second, independent architect: PASS with required changes, all applied below (2026-11-05).

**The waits (prerequisite).** One rule bounds every end: ending a transaction gets a deadline, and an end that *couldn't run* in time (nothing released, `write_word` still `Some`) is either abandoned to the owner's operation or *parked* for the end of the fork. It is never waited for without bound.

- `HeldShare::enter` while the owner is forking or parked (`gate.rs:603-621`): add `HeldShare::enter_until(deadline) -> Option<SharedGuard>`, the same loop with the `OwnersFork` sleep also ending at the deadline. `HeldTransaction::end` (`store.rs:1384`) uses it; on `None` it returns an internal `EndOutcome::NotEnded` (not `Error::Timeout`) with nothing released. This wait exists only while a fork is pending (`gate.rs:618`), so a draining fork end is coming (`Gate::defer`, `gate.rs:321-329`).
- An inherited share (`gate.rs:609`, `ShareEntry::Inherited`): the same method calls `gate.enter_shared_until(deadline)` instead of `enter_unbounded`. That wait also exists only behind a pending fork (`gate.rs:253-260`).
- A commit from another thread waiting for the owner's operation (`store.rs:1417-1427`, the `LENT_POLL` loop): this waits on the owner's operation, not a fork, so it never parks. At its deadline it takes the abandon branch above it (`store.rs:1405-1416`: the operation rolls back when it hands the connection back) and returns `Error::Timeout`. Only a timed-out *re-entry* (`enter_until` returning `None`) inside that loop parks.
- **Parking keys on `NotEnded` only.** `close_pinned`'s COMMIT can itself return `Error::Timeout` (SQLITE_BUSY/LOCKED, `error.rs:120`, `store.rs:1513`) after `end` has already released everything (`store.rs:1441-1449`). That is a finished, failed commit: report it, never park it. On `NotEnded`, `park` moves the live fields into a new `HeldTransaction` (the husk keeps `write_word: None`, so its drop is a no-op), **drops its share first** (below), and calls `GATE.defer(|| DEFERRED_ENDS.push(t), || run_blocking(&drain_deferred_releases))`, with `DEFERRED_ENDS: TakeAllStack<HeldTransaction>` beside `DEFERRED_OWNERS` (`store.rs:85`). A parked commit returns `Timeout`; a parked rollback returns `Ok`.
- **A parked end holds no gate level** (proven in the models, 2026-11-05). Keeping the share's level while parked deadlocks: the owner's fork ends before the end is recorded, so its after-fork drain sees no flag; a third thread claims a fork; the parker's try fails behind it; and that fork, not the owner's, waits forever for the parked level, which only a drain at its own end could release (model 18 found it; model 19 pins it). So `park` drops the share before it records, keeping the write lock and the D-020 hold: other writers still wait, and a child forked before the drain is poisoned for that file (D-020) instead of being kept out by quiescence, which is safe because no thread is inside SQLite on that connection. `end` and `abandon` on a parked transaction (share `None`) enter with a plain `GATE.try_enter_shared()`, which is reentrant because only the drain, on its own level, ends one. So a drained end can't fail; the drain's push-back is an unreachable release fallback (it asserts in debug builds). Every deferral then holds no level, as handle releases don't, so `Gate::defer`'s contract (the next fork end drains) holds unchanged.
- **The drain drops each parked transaction** (`drain_deferred_releases`, `store.rs:1586`), first, on its own level and outside the registry mutex, then does the releases. Dropping, not `finish(false)`, matters: if the owner forked from inside an operation, `end` reports `EndInsideOperation` and `Drop` routes it to `abandon` (`store.rs:1474-1479`). In a child the drain finds the generation changed and forgets the connection (`close_pinned`, `store.rs:1496`).
- **Between a park and the drain the transaction is still the owner's.** The slot stays `Here(owner)`, so whenever the owner can enter the gate, `held_by_this_thread` is true, a nested `transact()` passes through, and its writes land in the transaction that will roll back. Having given the share up, the owner holds no level, so behind another thread's fork its calls wait like anyone's, and two orderings are legitimate. If the owner gets back in after that fork ends but before its drain runs, `on_pinned` succeeds and the write joins the doomed transaction, which the drain rolls back. Otherwise `on_pinned` refuses, the write waits for the write lock within its `timeout`, and once the drain has run it commits on its own, as after any cross-thread end. Neither is a silent loss: the owner's own `__exit__` reports a transaction that didn't commit (3c, below), and the store test accepts both.
- **Budgets.** An explicit commit gets the transaction's `timeout` (`HeldTransaction.timeout`). `Drop` (a GC finalizer) and a cross-thread rollback have no caller who needs the outcome, so they try once and park, rather than block a finalizer for up to `timeout`.
- **Accepted, documented, unchanged:** `Gate::begin_fork` itself (Python can't refuse a fork, D-027), and `HeldShare::enter` from `abandon` (`store.rs:1458`), which runs only on the owner thread inside its own operation, where `try_enter_shared` is reentrant and never waits. Assert that precondition with `debug_assert!`: this thread's `LENT` list holds this store's key, or its guard depth is > 0 (a `cfg(debug_assertions)` accessor; `own_depth()` includes held levels, `gate.rs:367-370`, so it proves nothing). Rewrite `gate.rs:36-41` and the `tests/DIVERGENCES.md:224` bullet to say this.
- Add a compile-time assert that `HeldTransaction: Send + Sync` (`TakeAllStack` is `Sync` only when `T` is, `stack.rs:22`).

**Rust surface (`crates/diskcache-oxide-py/src/cache.rs`).** `WriteHold` (`cache.rs:903-947`) becomes `Transaction`: `cache: Py<Cache>`, `retry: bool`, `state: Mutex<TxState>` with `TxState { Ready, Entering, Held(HeldTransaction), PassThrough, Done { committed: bool } }`. `Cache.transact(retry=False)` returns one unentered (`retry` extracted as a strict `bool`, as D-014 does). `__enter__`:

1. Under the mutex, `Ready` → `Entering`, then release the mutex. Anything else raises `RuntimeError`, including `Entering` (a concurrent `__enter__`); the state is never held across the detached `begin_hold`.
2. `cache.store.held_by_this_thread()` (`store.rs:1134`) → `PassThrough`. Checked here, not in `transact()`, and per store, so a second handle on the same directory passes through too. An inherited transaction doesn't count, so a child's nested `transact()` raises `Unwritable` from `check_writable` (`store.rs:1150`; a `RuntimeError`, D-020).
3. Otherwise loop: `ensure_open`, then `detach(begin_hold)`. On `Error::Timeout` with `retry`, `py.check_signals()?` (attached, outside `detach`) and try again. Never retry anything else.
4. Store `Held` or `PassThrough`; on any error, put back `Ready` before raising. Return `self.cache` (the `tx`). The cache, not a proxy: only the owner thread's calls join the transaction, through any handle on the store, so a proxy would be a second API with no new guarantee. diskcache yields `None`, so this is a superset.

`__exit__` takes the state out of the mutex, then acts. `Entering` raises `RuntimeError`. `PassThrough` → `Done { committed: true }`, no commit (`return false`). `Held` → `finish(exc_type.is_none())`, then `Done { committed }` with `committed` true only for a commit that returned `Ok`; an `EndInsideOperation` leaves `Done { committed: false }`. `Done` (a second `__exit__`, or the owner's exit after another thread ended it) with no exception raises `TransactionAborted` (the existing core error, a `RuntimeError`) unless `committed`; this replaces today's `abandoned` flag, which returns `Ok(false)` when another thread ended it (`cache.rs:937-944`). A GC drop of a `Transaction` runs `HeldTransaction::drop` (abandon inside an operation, park behind a fork). New exception in `lib.rs`: `create_exception!(diskcache_oxide, TransactionBusy, Timeout, ...)`, with `Error::WriteLockBusyInTransaction` mapped to it (`lib.rs:75`). Export it from `__init__.py` and `__all__`. `_hold_write_transaction` returns `Transaction` with `retry=False` until 3e removes it.

**Python surface.** `TransactionalCacheLike(CacheLike, Protocol)` with `transact(self, retry: bool = False) -> AbstractContextManager[Self]` (`protocol.py`), exported beside `CacheLike`; `CacheLike` itself gets no `transact`, because FanoutCache's shard-scoped `transact` takes a key (plan 13, section 5). `NullCache.transact` checks `retry` is a `bool` and returns `contextlib.nullcontext(self)`. With `invalidate=` it does **not** open a transaction on the real cache: a switched-off cache must never take a write lock or raise `Timeout`, so forwarded removals inside the block land at once and an exception doesn't undo them (say so in `null.py`'s docstring). `_core.pyi`: `class Transaction` (`__enter__ -> Cache`, `__exit__ -> bool`, documenting `RuntimeError` for re-entry and `TransactionAborted`'s `RuntimeError` on an exit after the transaction was ended elsewhere), `Cache.transact`, `TransactionBusy(Timeout)`.

**Divergences.** D-020 becomes pinned (drop "when `transact()` lands"). Update D-027's waits bullet and D-028's "starting a transaction" to name `transact()`. New: **D-029 `transact()` hands back the cache, and every handle on the directory shares it** (diskcache: `None`, and a second handle's `BEGIN` waits out the timeout). **D-030 A transaction on a second, busy cache fails at once with `TransactionBusy`, and `retry=True` retries only `Timeout`** (diskcache: waits, and `retry=True` loops on any `OperationalError`). It's interruptible only between attempts: each attempt waits up to `timeout`, detached. **D-031 A transaction may end on another thread, within its `timeout`** (diskcache: "may not be shared between threads"): a commit that can't reach it raises `Timeout` and rolls back, and the owner's own exit afterwards raises `RuntimeError` (`TransactionAborted`) unless it committed.

**Build order** (each is one commit through `scripts/check.sh` on all four interpreters):

- **3a, core bounds** (`model-check.sh`): the waits above. Tests: replace `a_share_entered_during_its_owners_fork_waits_with_no_deadline` (`gate.rs:1276`) and the inherited one after it with deadline versions. Store tests: an end elsewhere during the owner's fork (parked, rolled back at the fork's end, write lock free after); the owner writing between the park and the drain (it stays held by the owner, and the writes roll back); a commit waiting on the owner's operation that times out and is abandoned, not parked; a COMMIT that fails busy is reported, not parked; a parked end drained in a "child"; a drain that finds the slot lent to the drainer (the owner forked from inside an operation: `Drop` abandons). Plus `a_parked_end_never_holds_up_a_later_fork_by_another_thread` (the owner's fork ends with no drain, another thread's `begin_fork` must return, and its drain rolls back), and every park test asserts `GATE.own_depth() == 0` while parked. Shuttle models in `gate/model.rs`, all ending in `assert_idle` (no parked end, the write lock free, the gate idle): 17, an end parked behind its owner's fork is always drained; 18, the owner parked behind a third thread's fork, the drainer's try failing because the owner claimed the next fork, then the owner's drain; 19, the regression: the parker's try failing behind a third, non-owner fork after the owner's drain looked, which must finish; 20, the push-back race: an end recorded while a drain is past its take, never stranded. The proven code is `scratchpad/park/protocol-on-top-of-3a.diff` (see Progress).
- **3b, `TransactionBusy`**: the mapping and export. Test: through the hook, the cross-cache refusal raises `TransactionBusy`, an instance of `Timeout`, in under 1 s.
- **3c, `Cache.transact`**: `Transaction`, stubs, D-029 to D-031, and `'transact'` added to `OXIDE_SUPPORTED_FEATURES` by name (`tests/conformance/backends.py:44`; don't bump `OXIDE_PHASE`, which would turn on 6a). Conformance `test_transact.py` (`requires('transact')`): commit on exit, rollback on exception, reentrancy, an inner exit doesn't commit (a second thread's read sees nothing until the outer exit), and a caught inner exception keeps the inner writes. Unit `test_transact.py`: `tx is cache`; the same-store pass-through; re-entry, a concurrent `__enter__` and a non-bool `retry` raise; a double `__exit__`; the cross-cache refusal with `retry=True`; fork from a non-owner thread mid-transaction, and from the owner (D-020, in `fork_scenarios.py`); commit and rollback from another thread while the owner is inside an operation (a pickle hook that waits), including the timeout; the owner's exit after a cross-thread rollback raising `TransactionAborted`; a GC finalizer dropping a manually entered `Transaction` inside an operation (the `abandon` path); a durable handle whose `BEGIN` times out, and a failure inside the block, both raising; another coroutine's write during an `await` rolling back with the block.
- **3d, `NullCache`, `TransactionalCacheLike`, `Caches`**: tests in `test_null_cache.py` (including `invalidate=`), `test_caches.py` (a disabled cache's `transact()`), `test_stubs.py`.
- **3e, remove the hook**: move every `_hold_write_transaction` user (tests, `soak/worker.py`, `bench/scaling/batch_bumps.py`) to `transact()`, make the conformance backend call it unconditionally, delete the method and stub.
- **3f, docs** (`model-check.sh` once more): the user guide's transactions section (thread, not task; the fork waits; lock order; an end from another thread), `switching-from-diskcache.qmd`, and the soak note.

**Risks.**

- Never hold `Transaction.state` while finishing or beginning: that mutex is taken attached, and `finish` waits with the interpreter released (today's pattern, `cache.rs:921-925`). `Entering` is what keeps a second caller out meanwhile.
- Park only on `NotEnded`. An `Error::Timeout` out of `end` may be a finished, failed COMMIT whose locks are already released (`store.rs:1441-1449`); parking that husk would park nothing and hide the failure.
- `__enter__` must not go through `attempt`/`store_failed` (`cache.rs:534-551`), or a durable handle would swallow `BEGIN`.
- `park` drops the share *before* it records, and records before it flags (`gate.rs:321-329`). A parked end that keeps its level deadlocks the next non-owner fork (model 19). A drained end can't fail: on a parked transaction `end` enters on the drainer's own level, so the drain's push-back is an unreachable fallback.
- Keep `end`'s release order: the gate dropped before the write lock, then the D-020 hold, then the share (`store.rs:1445-1449`; `Lend::drop`, `store.rs:323`).
- Lock order stays write lock, then gate, then connection (`store.rs:9-16`); `begin_hold` keeps `try_lock_nested` at depth > 0 (`store.rs:1166`).

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
- 2026-11-05: step 2 closed, no change. With step 1 in, `incr` is 14.2 µs on oxide against 16.9 µs on stock (median of 5 interleaved runs), and per-commit `pwrite` time is within noise on both. Ruled out: the C flags (cc's `-O3`; `-O2`, `-Os` and dropping `SQLITE_ENABLE_API_ARMOR` all measure the same), and SQLite 3.54.1 against 3.53.2. A raw `BEGIN IMMEDIATE`/`UPDATE`/`COMMIT` loop is 5.3 µs on the bundled 3.54.1 against 6.1-6.4 µs on CPython's 3.53.1. The earlier 2.8 µs gap was likely run-to-run noise or the pre-step-1 state.
- 2026-11-05: step 3a's shuttle model 18 caught a deadlock in the first parking protocol, which kept the parked end's share level: after the owner's fork ended without a drain, a third thread's fork waited forever for that level (livelock assert, task-3). Redesigned so a parked end drops its share before it records and is ended on the drainer's own level, so nothing ever waits for a parked end and a drained end can't fail (the push-back is an unreachable fallback). Proven in a scratch copy: `scripts/model-check.sh` at 100k schedules per scheduler, 22 passed (models 17-20 new or rewritten, each ending in `assert_idle`); the old protocol, restored as a control, fails models 18, 19 and 20 with the livelock; core `cargo test` 200 passed; clippy clean. The failing schedule can't be replayed exactly against the changed code (shuttle reports the schedule diverged), so model 19 is the regression. Diff on top of the 3a working tree: `/private/tmp/claude-501/-Users-michaelkennedy-github-talk-python-talk-python-to-me-src-talk-python-to-me-com/27c1bfc1-0395-47ae-8c3b-0f287db56b43/scratchpad/park/protocol-on-top-of-3a.diff`.
- 2026-11-05: step 3a done. Every wait that ends a held transaction is bounded by its `timeout`; an end that can't run in time drops its gate level, parks, and is rolled back by the next fork end's drain. A fresh architect review passed it (no correctness issues; doc, plan and test drift fixed). `scripts/check.sh` passes on all four interpreters, 201 core tests, 23 shuttle models at 100k schedules.
- 2026-11-05: steps 3b and 3c done. `with cache.transact() as tx:` is public, with `tx` the cache itself, per-store reentrancy, `TransactionBusy` never retried, and `TransactionAborted` when another thread rolled the block back. The fresh review found two bugs the suite missed, both fixed with regression tests that fail on the old code: an owner exit overlapping another thread's commit reported a false rollback (20 of 20 in a race script; now an `Ending` state waits up to `timeout` for the true outcome), and a `Transaction` in a reference cycle was never collected, holding the write lock forever (now `__traverse__`). `scripts/check.sh` passes on all four interpreters.
- 2026-11-05: steps 3d and 3e done. `TransactionalCacheLike` is exported (`CacheLike` unchanged); `NullCache.transact()` is a no-op that never opens a transaction on its `invalidate=` cache; `Caches` lookups are typed `TransactionalCacheLike` so `caches[name].transact()` type-checks in app code; every hook user calls `transact()` and the hook is gone. `scripts/check.sh` passes on all four interpreters.
- 2026-11-05: step 4 decision (Michael): the app gets local `0.1.0.dev2` PGO wheels (macOS 3.14 and 3.15, Linux amd64) in its `wheels/` folder via `--find-links`; nothing goes to PyPI, and the branch stays unpushed until Michael says.
- 2026-11-05: step 3f done, so step 3 is complete. The user guide has a Transactions page (thread not task, the fork wait, lock order, TransactionBusy, TransactionAborted and the Ending wait, durable), with matching updates to switching-from-diskcache, divergences, concurrency-and-forks, null-cache and the roadmap; `soak/README.md` records that steps 1 and 3 need a fresh 24-hour soak before production. Site regenerated. `scripts/check.sh` passes on all four interpreters.
