# Plan 09: a `pop` that keeps foreign values, and `durable`

Two follow-ups from plan 08, both Michael's decisions (2026-10-30). He also confirmed the name `ForeignValueError`. Each step gets the architect's review once built, and the suite runs on all four interpreters.

**The soak's scope.** The 24-hour run is on commit `0f1b9b8`. Plans 08 and 09 change `get_with` and `pop` after that commit, so its PASS doesn't cover them. The soak worker doesn't call `pop` at all today, so the kit needs it added. A shorter soak of the final code should run before Phase 3 goes to production. See step 3.

## Step 1: `pop` decodes before it deletes

**Problem.** `Store::pop` deletes inside its write transaction and decodes after the commit (decoding can run a pickle hook, which must not run inside the transaction). So a value in a known mode with an unknown tag or extension, or a missing time zone, is destroyed by a `pop` that can't read it. A newer peer's data is lost during a deploy, and under `on_decode_error='raise'` the caller gets an error with the row already gone.

**Design.** `Cache::pop` becomes optimistic:

1. Read and decode with `get_with`, exactly as `get` does: no write lock, and hooks run where a `get`'s hooks run. A miss returns the default.
2. On a clean decode, delete exactly that version with a new blocking `Store::delete_version(timeout, version) -> Result<bool>` (through `write_files`, so a held transaction defers the file's unlink as `pop` does today).
   - `true`: this caller removed it, so it returns the value. Two racing `pop`s can't both get `true`, so a value still goes to one caller.
   - `false`: another writer replaced or removed it first, so go back to 1, up to `POP_ATTEMPTS` (8, as `spill::OPTIMISTIC_ATTEMPTS`).
3. After that many losses to overwrites, fall back to today's locked `Store::pop`. It can't starve, and only then can a foreign tag be destroyed; the docs say so.

**When the decode fails:**

| Failure | `'raise'` | `'miss'` / callable |
|---|---|---|
| Foreign, `UnsafeValueError` | raises, row kept | Foreign: default, row kept. Unsafe: raises, row kept |
| Unreadable (malformed bytes, a failing unpickle) | deletes that version, then raises (today's "a value that fails to decode is still removed") | deletes that version, returns the default, reports |
| Passthrough (`Timeout`, `MemoryError` and so on from a hook) | row kept, raises | row kept, raises |

The last row changes today's behavior for the better: a transient error no longer costs the value. Under `'raise'` the delete is blocking, as `pop` is today, and a `Timeout` from that delete replaces the decode error.

**Costs and checks:**
- `pop` is now a read plus a write. It's off the gate's list and isn't a hot path, but measure it (`pop` of a small value, before and after).
- `refuse` and `Pop::Refused` stay for the fallback.
- Inside a held transaction, the read and the delete both use the transaction's connection.
- A D-020 child reads, then the delete raises as `pop` does today.
- Hooks now run before the row is gone, so a hook that overwrites its own key loses the race, retries and eventually takes the fallback. Bounded.

**Tests:**
- a foreign tag and a missing zone under both policies: row kept;
- N threads and processes racing `pop` over many keys: each value returned exactly once;
- `pop` losing to an overwrite returns the newer value or falls back;
- a hook raising `Timeout` under both policies keeps the row;
- the two existing hook tests (`test_pop_decodes_after_its_write_commits` is renamed, since the order is the point) and the held-transaction test;
- `test_pop_of_a_foreign_tag_says_it_was_deleted` flips to "kept".

## Step 1b: a pending fork can't outlast `timeout`

Design only, written on `a79f895` and revised after the architect's review (2026-10-30), which found no corruption or count-leak interleaving and required the changes now folded in here. Nothing here is built. Line numbers are for that commit. It lands before step 2, which assumes calls return. Michael decided on 2026-10-30 that the wait honors `timeout`, so this section only settles how. The review's experiments are in the session scratchpad (`arch/`): `ticks.py`, `hammer.py`, `lockhold.py`, `importstuck.py`.

**The problem.** `Gate::enter_shared` waits without limit while a fork is pending, and `Gate::begin_fork` waits without limit for gate holders to drain. A thread holding a level (a held transaction, or a pickle hook on an inline value) that depends on another thread's cache call wedges the process's whole cache layer: that thread waits for the fork, the fork waits for the level, the level waits for that thread.

**Reproduced by both routes.** The held-transaction route at the Rust level (`repro_fork_pending_when_the_ender_enters` in the scratch copy). The pickle-hook route was only reasoned until now. It reproduces from Python on 3.14 and 3.14t with `cache.timeout = 1.0`: a `__reduce__` rebuild function on an inline value starts a fork on one thread, then a call on a third thread, and waits up to 4 s for it. On 3.14, `get`, `set`, `close()` and `Cache(...)` on a different directory each had not returned after 4 s, with the fork still stuck; on 3.14t, `get` (the only call run there) did the same. The fork finished as soon as the hook gave up and returned. The extension was built at 22:30 from the tree that became `a79f895`, and `gate.rs` hasn't changed since `d3cbbb0`.

### The mechanism

Add one bounded entry, and remove the unbounded one from everything but one private path:

```rust
/// Enters in shared mode, waiting through the blocker while a fork is pending, until `deadline`.
/// `None` means the deadline passed behind a pending fork. A waiter holds no level and no count, so
/// there's nothing to undo.
#[inline]
pub fn enter_shared_until(&self, deadline: Instant) -> Option<SharedGuard<'_>> {
    match self.try_enter_shared() {
        Some(guard) => Some(guard),
        None => self.enter_shared_slow(deadline),
    }
}
```

The `#[cold]` slow path loops: if `now() >= deadline`, count it and return `None`; wait through the blocker while `pending` is set and the deadline hasn't passed, sleeping `min(WAITER_POLL, remaining)`; try again. So it always tries once more after a wait. Deadlines use `crate::sync::now()`, so the shuttle clock drives them. `wait_while_pending` goes private and gains a sibling, `wait_while_pending_until(deadline)`; the no-deadline form stays for `enter_unbounded` and `enter_shared_blocking`.

`Gate::enter_shared` becomes a private `enter_unbounded`, used only by `HeldShare::enter` (row 15, the `Inherited` arm). Tests move to the existing `enter_shared_blocking`. The compiler then rejects any new call site with no deadline, which is a stronger check than a source scan.

**Rejected:**
- A `Duration` argument: it needs a clock read at every call, including the fast path.
- A thread-local "current deadline": hidden state that a hook's nested call would inherit.
- A condvar to wake waiters: the module rules out anything that a fork could inherit locked or that loses a wakeup.

### 1. Every call site

| # | Site | Timeout to honor? | What it does after this step | Why it can't recreate the wedge |
|---|---|---|---|---|
| 1 | `store.rs:341` `Store::open`, each attempt | Yes, `options.timeout` | `enter_shared_until(attempt_deadline)`, inside `lockwait::retry` | Raises `Timeout`, holding nothing |
| 2 | `store.rs:349` `Store::open`, registry insert | Yes, but it must not fail: it holds `conn` and the pin, which must close under the gate | The site goes away. The attempt's closure returns its `SharedGuard` too, so the same level covers the insert. `canonicalize` moves before the retry, so the level isn't kept across it | No wait |
| 3 | `store.rs:400` `Store::lookup` | Yes | Takes the open's deadline and returns `Result<Option<_>>` | Raises `Timeout`, with no handle claimed |
| 4 | `store.rs:527` `add_handle` (a closed handle used again, through `ensure_open`) | Yes | See "Reopening" below: enters first, then flips `closed` | Raises `Timeout`, with the handle still closed and nothing counted |
| 5 | `store.rs:540` `release_handle` (`close()`, `Drop for Cache`) and its call at `store.rs:515` (`join`'s failure path) | None it can raise from | See "Closing" below: a flat 1 s, then a deferred release. `join`'s failure path tries once and defers, with no wait | Returns within 1 s whatever the fork does |
| 6 | `store.rs:604` `read`, fast path | Yes, but no clock here | `try_enter_shared()` in the `if let` chain after the slot. `None` drops the permit and falls through to row 7 | No wait |
| 7 | `store.rs:612` `read`, full loop | Yes | `enter_shared_until(attempt_deadline)` | Raises `Timeout`; slot released by RAII |
| 8 | `store.rs:728` `write_unpinned`, each attempt | Yes, the write's one deadline | `enter_shared_until(attempt_deadline)` | Raises `Timeout`; slot and write lock released by RAII; a spilled value's file is discarded by `settle_with` as on any failed write |
| 9 | `store.rs:855` `checkpoint_once` | Its callers treat busy as an answer | `try_enter_shared()`; `None` is a new `Outcome::ForkPending`. It isn't `Busy`: that would start the 1 s `RETRY_AFTER_BUSY` backoff (`wal.rs:215`) and count an ordinary fork as WAL contention. `record_outcome` counts it in its own `fork_pending` stat and leaves `next_attempt_ms` alone. `checkpoint(timeout)` retries on it to its deadline, then returns `False` (D-024: never raises for busy). `maybe_checkpoint` skips | No wait. It also stops `maybe_checkpoint` waiting behind a fork with the write lock held |
| 10 | `store.rs:929` `write_nowait` | None: "needs no waiting at all" | `try_enter_shared()?`, so `None` and `VersionDelete::Skipped` | No wait. Fixes `delete_version_nowait`, whose doc admits it waits |
| 11 | `store.rs:951` `on_pinned` | None here, and none needed | `let gate = GATE.try_enter_shared()?`. `None` proves this thread has no transaction on this store: an owner has a held level, and a thread inside an operation has depth > 0, and either is let through. So it isn't pinned, and the caller's normal path (rows 7 and 8) applies its deadline. Only reached while `pinned_active` is set | No wait. This is the site the wedge hits when the other thread uses the same store |
| 12 | `store.rs:995` `begin_hold`, the nesting check | Same as row 11 | `if let Some(gate) = GATE.try_enter_shared()`. `None` proves it isn't nested | No wait |
| 13 | `store.rs:1013` `begin_hold`, each attempt | Yes | `enter_shared_until(attempt_deadline)` | Raises `Timeout`; write lock released by RAII |
| 14 | `store.rs:1025` `begin_hold`, install | Never waits: this thread holds the level from `into_held` | `try_enter_shared()`, `unreachable!` on `None` | No wait |
| 15 | `store.rs:1233` `end`, `store.rs:1296` `abandon`, through `HeldShare::enter` (`gate.rs:480`, `gate.rs:492`) | `finish` has one, `Drop` doesn't | Unchanged, except the `GATE.enter_shared()` fallback, which is dead (`write_word` is `Some` only while `share` is), goes. See "The waits left" below | The plain route never waits (it's what fixed `end_mid_operation`). **The owner's-fork wait and the `Inherited` arm can still be part of a cycle**, as described there |
| 16 | `store.rs:1407` `FilePin::drop` | None; can't fail | `try_enter_shared()`, and `mem::forget(conn)` on `None`: one fd leaked. A backstop only: `release_handle` takes the pin under a level, and a deferred release keeps the store alive until its drain does so (see "Closing") | No wait. A pin holds no lock and runs no statement, and never closing is the safe direction |
| 17 | `pool.rs:274` `Pool::drop` | None; can't fail | `try_enter_shared()`. On `None`, `mem::forget(Arc::clone(&self.shared))` and return, with nothing closed outside the gate. The registry's `Weak` stays alive, so the pending fork's `close_idle_all` closes the idle connections with the gate held exclusively. Cost: one empty `PoolShared` and a registry slot, for each pool dropped during a pending fork | No wait, and the connections still close correctly |
| 18 | `store.rs:1536` to `2147`, `pool.rs:305` to `360`, `gate/model.rs` (tests) | n/a | Renamed to `enter_shared_blocking`. Same behavior | Tests only; each stuck-fork test has a watchdog (section 7) |

Nothing else in core enters the gate. `poison.rs` uses `try_enter_shared` in its tests only, and the other waits (`lockwait::backoff`, `Slots`, `WriteLock`) already have deadlines.

**Reopening (row 4).** `ensure_open` swaps `closed` to false, then counts the handle. A wait that fails after the swap would leave the count one too low once another thread's `close` released it. So the order flips: load `closed` (false is the usual case: one `Acquire` load, cheaper than today's unconditional swap), and only if it's true take a deadline, enter the gate, and swap and count under that level. A failure before the swap changes nothing. `ensure_open` becomes `Result<Duration>`, returning what's left of the budget, and every operation passes that on.

**Closing (row 5).** Settled: `close()` and `del` return after the budget and defer. `close()` can't raise (it's documented never to fail) and `Drop` can't. So `release_handle(self: &Arc<Self>)` tries `enter_shared_until` for a flat 1 s of its own. It doesn't depend on `timeout`, because `min(timeout, 1 s)` would defer on ordinary forks with small timeouts, and it isn't shared with `checkpoint_on_close`, which could leave the release nothing. So `close()` takes at most about 2 s behind a stuck fork: 1 s for the checkpoint, 1 s for the release. An ordinary fork ends well inside that (section 2 has the numbers), so `close()` waits it out as today and D-018 still holds when it returns. If the fork outlasts the second:

- **Keep the store alive:** `mem::forget(Arc::clone(self))`, as row 17 does for the pool, so the store stays in the registry even if this was its last `Arc`, and the drain can release it normally and close the pin. Cost: one retired `Store` and a registry slot per event. A later open of that directory revives it, as with any retired store.
- **The deferrer**, in this order: add one to the store's new `deferred_releases: AtomicUsize`; set the gate's `deferred` flag (`SeqCst`); then `try_enter_shared`, and drain if it gets in. That last step closes the race with a fork that ended just as the deferral was recorded.
- **The drain**, in this order: load the flag; `try_enter_shared`, and if that fails return with the flag still set; only then clear the flag and collect the stores. Clearing first strands a release: fork 1's drain clears the flag, its try fails because fork 2 has claimed, and fork 2's end sees no flag. With this order, a release added after the collection set the flag after the clear, so the flag stays set and its deferrer tries for itself.
- **Releasing:** under the level, collect `Arc`s for the stores with a non-zero count, release the registry, then for each: swap its count to 0 and release that many handles through the same code as `release_handle`, with a pin closed outside the registry mutex. Collecting first means an `Arc` dropped here can never close a pin under that mutex.
- **Who runs it:** `_after_fork_in_parent` and `_after_fork_in_child`, after the fork has ended, and through `crate::detach` when the flag is set, since closing a last connection can run SQLite's own checkpoint. No cost anywhere else. A deferred release can only exist because a fork was pending, so that fork's end, or the next one's if another claimed first, is coming.
- **Until the drain,** the store just looks like it has one more handle. The handle count is signed and decided by where it lands, so a reopen meanwhile balances. A child inherits the count and its own drain applies it, forgetting rather than closing the connections.
- **`join`'s failure path** (`store.rs:515`) runs after `Cache(dir, size_limit=...)` has already used its whole budget, so it mustn't spend 1 s more. It tries once and defers, which the keepalive makes safe. `join` takes `self: &Arc<Self>` for that.

Rejected: unbounded (it reproduces with `close()`); raising from `close`; a helper thread to do it (threads and fork); leaking with no drain (breaks D-018 for the rest of the process).

**The waits left (row 15).** Settled: these stay unbounded, and bounding them becomes a `transact()` design requirement in Phase 6b. They aren't safe in every case. No public API reaches them today, since `_hold_write_transaction` is a test hook until then. Bounding them needs somewhere to park an ended transaction, because forgetting one leaves the write lock, a level and SQLite's lock held for good.

- **`HeldShare::enter` while the share's owner is forking or parked,** because that fork doesn't wait for the share (`gate.rs:492`). A cycle needs a thread that isn't the owner ending the transaction, the owner itself in `os.fork()` or parked behind another fork, a third thread holding a level, and that third thread depending on the ender.
- **The `Inherited` arm** (`gate.rs:480`): an unbounded entry in a child, reachable by a child thread finalizing an inherited hold while that child has a stuck fork of its own.
- **`end`'s commit that waits for the owner's operation** to hand back the connection (`store.rs:1256`). Not a gate wait, and it needs no fork; recorded here because it's the same kind of hang.

### 2. One deadline

The gate takes the operation's existing deadline. It never gets a fresh `timeout`.

- **`read`, `write_unpinned`, `begin_hold`:** already one deadline; the gate joins the slot and lock waits on `attempt_deadline`, so `lockwait::retry` reports the whole `timeout`.
- **`pop`:** its one `Instant` is untouched. `read_row(left())`, `delete_version(left())` and the fallback's `left()` all reach the gate through rows 6 to 8, and `pop_error` relabels the timeout. A gate timeout before the delete keeps the row.
- **`get_with`:** the open follow-up below says each internal read and the D-016 write get the full value. Starting a new deadline at the first missing file isn't enough: the first read can spend 0.9 of `timeout` behind fork A in its slow path, and then a later read a full `timeout` behind fork B, which is 2× the budget. So the first read goes through a private `read` variant that also returns the deadline its slow path took, captured from `lockwait::retry`'s closure argument, or `None` if the fast path succeeded. That stays clockless. At the first missing file, the deadline is that one, or `now + timeout` after a fast-path success, which is right since the fast path has no wait to charge. Every later read and the D-016 write use what's left, relabelled to `timeout` on the way out. That closes the follow-up for `get`, and for `pop` through it.
- **`Store::open`:** one deadline at the top, covering `lookup`, the retry and `join`'s write. Today those are separate budgets, so an open can use two. Its failure path adds no wait (see "Closing").
- **Reopening:** `ensure_open` returns what's left, per row 4. On that rare path a timeout's message names the remainder rather than the whole timeout; accepted.
- **`expire`, `clear`, `cull`:** stay per chunk, as their docs say. Each chunk and the snapshot read has one deadline. A stuck fork fails the first blocked step, after that step's `timeout`, with earlier chunks kept.

**`timeout=0`.** The first `try_enter_shared` still runs, and a deadline already passed returns `None` with no wait. So a level held by this thread still gets in, and a pending fork means `Timeout` at once. This matches `lock_until` and `acquire_until`, which try first too.

**A consequence to document: `timeout` must exceed in-flight work plus the fork plus the interpreter hand-off.** Every ordinary fork now counts against `timeout`, and the pending window is much longer than the fork's own cost:

- **Quiet process:** `bench.fork_cost` (one cache, 8 idle connections, small process, nothing else running): parent side p50 1.07 ms, p99 1.69 ms.
- **Beside 8 busy threads on 3.14** (`hammer.py`): `os.fork()` p50 43 ms, max 344 ms, equally under a `timeout=0` hammer and under a pure-Python spin with no cache. So the delay is the forking thread re-attaching the interpreter after it detaches, not this library's quiescence. On 3.14t the hammer is harmless.
- **Levels in flight count too:** the fork waits for them, including a forced checkpoint (up to 1.4 s in the soak, `store.rs:868`).
- **What this step adds:** callers behind a fork now charge that window to `timeout`, and `timeout=0` callers stop sleeping and become the busy threads. With forks every 46 ms, about 59% of `timeout=0` calls got `Timeout`.

**Overshoot.** The deadline isn't exact: one `WAITER_POLL` at most in the wait, then re-attaching, which on a GIL build can take a switch interval per busy thread, as with every other detached wait. That needs the interpreter to be re-attachable, which section 4 states as an assumption.

### 3. The fast path

A `get` that hits does what it does today, bar the changes below. `enter_shared` was a loop whose first step was `try_enter_shared`, and its `Some` arm returned, so row 6 is the same atomics in the same order, with the blocker branch removed. No new atomic, no new field read. `enter_shared_until` takes an `Instant`, so the fast path, which has none, can't call it, and the type system keeps the clock off it. `on_pinned` still returns on `pinned_active` before any gate work. `get_with`'s first read adds an `Option<Instant>` to its return, with no clock read when the fast path succeeds. `ensure_open` gets slightly cheaper (load instead of swap), so a small gain would be that, not noise.

**Check:** `bench.gate` on 3.14 and 3.14t, on AC power, on the commit before and the commit after, interleaved as step 1's measurement was. `bench.gate` compares against diskcache rather than an old build, so compare oxide's `get_hit_small` p50 and ratio across the two commits. Pass: both stay GO, with `get_hit_small` within noise of before. Review the diff for any `now()` or `Instant::now` reachable from the read fast path. Run `bench.fork_cost` too, for the import change in section 4.

### 4. Why `begin_fork` stays unbounded, and what the guarantee is

CPython can't refuse a fork: it ignores exceptions from before-hooks. So a bounded `begin_fork` could only return early and let the fork proceed without quiescence, and then the child inherits half a transaction or SQLite's lock state. That's the D-012 stall at best and, per `parent-closes-after-fork`, a WAL deleted under the child at worst, which loses writes. A stuck fork is a visible problem; a corrupt child isn't. Rejected: giving up with a warning.

**The guarantee.** A cache call waiting on a pending fork returns within its `timeout` plus the overshoot above, raising `Timeout`. `checkpoint` returns False instead. `close` and `del` return within about 2 s and 1 s, whatever `timeout` is. `Cache(...)` raises within its one `timeout`, and a failed `join` adds no wait after it.

**The exceptions** are the three waits in "The waits left", all reachable only through a held transaction: `HeldShare::enter` while the owner forks or parks, the `Inherited` arm, and `end`'s commit.

**The assumption.** The overshoot needs the interpreter to be re-attachable. A hook stuck in C code holding the GIL means nothing returns, on any thread. Stuck on anything else, waiters still time out and the fork stays stuck.

**What it doesn't promise:**

- `os.fork()` itself may still never return, and any second `os.fork()` in the process queues behind it.
- While a fork is stuck, the cache is unavailable to every thread at depth 0, on **every** `Cache` in the process, since the gate is process-wide. Each call uses up its whole `timeout` (60 s by default) and raises. Threads already holding a level carry on, including nested calls in hooks, which never time out at the gate. Other processes aren't affected.
- The cycle clears only if the holder reacts. The other thread gets `Timeout`, and the holder must then stop depending on it: it receives the error, or gives up, and releases its level. A hook that retries forever, or one blocked on something that isn't a cache call, keeps the fork stuck, while everyone else keeps getting `Timeout`. What this step removes is the hang in the library, not the bug in the hook.
- Once the level goes, the fork proceeds with nothing to repair.

**A second wedge this has to fix: other libraries' before-hooks.** CPython runs before-hooks in reverse registration order, and `_core` registers ours at init. `logging` (`before=_prepareFork`, which acquires its module lock, on 3.14.10 and 3.15) and `concurrent.futures.thread` (`before=_global_shutdown_lock.acquire`) both take a lock in their before-hook and hold it until the after-hook. With the default order, where `import diskcache_oxide` comes first (and `caches.py` imports `logging` *after* `_core`), those hooks run first, so their locks stay held for as long as our hook waits. Measured with a held transaction and a stuck fork, on 3.14:

| Call on another thread, while the fork is stuck | `diskcache_oxide` imported first | `logging` and `concurrent.futures.thread` imported first |
|---|---|---|
| `logging.getLogger('new name')` | No return in 4 s | 0.09 ms |
| first `.warning()` on `diskcache_oxide`'s logger | No return in 4 s | 0.3 ms |
| `ThreadPoolExecutor.submit` | No return in 4 s | 0.2 ms |

So a hook that logs or submits work never gets to react, and step 2's "log the swallowed failure" would hang on its first call, defeating this whole step. **Fix (settled):** `register_fork_hooks` imports `logging` and `concurrent.futures.thread` after its `hasattr` check and before registering, so ours runs first and those locks are only taken once the fork has quiesced. Import errors are ignored, so a failed import can't stop the hooks registering. `logging` costs nothing extra (the package imports it anyway). `concurrent.futures.thread` adds about 1.6 ms (the review's measurement of the import delta) to an import of about 13 ms. Rejected: registering at the first `Cache` (changes the tested hook order, and still loses to anything imported later). A third-party hook that takes a lock and is registered after ours can't be fixed from here; the guide says so.

**This order has its own cost, and it must not land first.** It moves every app to the logging-first order, which has a bounded inversion: a thread calling the cache while holding `logging._lock`, behind a fork, holds up logging's before-hook until that thread gets `Timeout` at its bound (`lockhold.py`). Logging-first apps hang forever on this today. So the hook order lands only after the bounded entry, per the build order, and the guide gets a line about it.

### 5. The error

Reuse `Error::Timeout`. `enter_shared_until`'s `None` becomes `Error::Timeout { seconds: 0.0 }` at the call site, as `reserve_slot` does, and `lockwait::retry` (or `pop_error`, or the mapping in `Store::open`) puts the real number in. `to_py_err` already makes it `diskcache_oxide.Timeout`, a `TimeoutError`. Rejected: a new variant, which would need its own arm in `retry`, in `write_unpinned`'s mapping, and in `pop_error`, for a distinction no caller should branch on.

The message stops being wrong: `"timed out after {seconds} s waiting for a lock or a pending fork"`. A grep of tests, Python and the guide finds nothing pinned to the old wording. To tell the cases apart when diagnosing, the gate counts these timeouts (`fork_timeouts`, on the cold path only), exposed with `pending`, `holders` and `parked` through a `_gate_stats()` test hook like `_wal_stats`.

**Step 2.** `Timeout` is already operational in `is_operational()`, and no new variant means no new row. So under `durable=True`, the table applies as written: `get` and `in` give the default or False, `set` gives False, and so on. Changes to step 2:

- A gate timeout can't happen inside a held transaction, or inside a hook, since both pass reentrantly. So "anything inside a held transaction raises" never sees one.
- `ensure_open`'s error goes through the same store-seam helper, so a durable `get` on a closed handle is a miss.
- The constructor still raises `Timeout`, as the review's table says. `close` and `del` never raise, durable or not.
- The review's open decision on `classify` matters more now: if a hook's other thread gets `Timeout` and the hook re-raises it, a durable `get` turning that into a miss (the recommendation) clears the cycle with no further code from the caller.
- "Durable doesn't bound time, so pair it with a small `timeout=`" gets section 2's caveat: `timeout` must still exceed in-flight work plus the fork plus the interpreter hand-off, or forks give misses. On a busy GIL build that's tens to hundreds of milliseconds.
- **A new test injection:** a stuck fork is the deterministic way to make `get` and `in` fail. The review noted a peer holding the lock can't. Use it for step 2's tests.
- The reporting must use the hook-order fix above.

**`Caches`.** A first lookup colliding with a fork can now get `Timeout` on a small `timeout`. `Timeout` is an `OSError`, so it's in `_OPEN_ERRORS` (`caches.py:54`), and that means a `NullCache` for `retry_open_after` (10 s by default) plus an `ERROR` log with a traceback (`caches.py:214-218`), for what's only a busy moment. Lookups of a cache already open are dict hits and never touch the gate, so this is about first opens and retries.

Recommendation: treat `Timeout` differently from a real open failure. `_resolve` catches it first and remembers the failure for `min(retry_open_after, 1 s)` rather than the whole `retry_open_after`, and the default report logs at `WARNING` with no traceback; a custom `on_open_error` still gets the exception. It still returns a `NullCache`, and it still remembers the failure, since retrying on every lookup would make each one spend the whole `timeout` behind a stuck fork. Python can't tell a fork's `Timeout` from a peer holding the lock, and needn't: both are "busy". Rejected: no memory at all (that cost per lookup); leaving it as it is (10 s without a cache, and an `ERROR`, for one ordinary fork). Document it in `user_guide/caches.qmd`.

### 6. Interactions

- **`t.forking`:** set only after quiescence, and `try_enter_shared` lets that thread through, so no wait and no timeout for the forking thread between hooks. Before our hook has claimed, another library's before-hook using a cache behind another thread's claim now gets `Timeout`, which CPython ignores, where it would have hung.
- **Early child entry:** `enter_child_early` runs inside `try_enter_shared`, before the check, so it resets the gate and the first try gets in. The slow path is never reached. A child whose before-hook never ran, or that came from a raw `fork()`, inherits `pending` set: until the after-hook, a call used to hang there forever, and now it times out.
- **A fork finishing as the deadline passes:** either outcome is fine, and neither leaks. The last try can succeed slightly late (the level is counted once, and the guard releases it), or fail on a new claim (its `fetch_add` and `fetch_sub` are one balanced call). A waiter holds no level and no count during its wait, and the guard and `t.depth` exist only on success, so the timeout path has nothing to undo. An unwind from the blocker is the same, and a permit or token is RAII.
- **The parked count:** untouched. A thread with levels never reaches the wait, since its try succeeds; a parked thread is in `begin_fork`, not here. Only `park`, `unpark` and `HeldShare::drop` write the high half.
- **Quiescence:** a failed `try_enter_shared` adds a transient level, as today. But today's caller then sleeps until the fork ends, while a caller that gets `Timeout` can come straight back, so a `timeout=0` loop repeats it. Measured (`hammer.py`): that doesn't hurt quiescence. A fork beside a `timeout=0` hammer takes as long as one beside a pure-Python spin with no cache, and on 3.14t the hammer is harmless. The cost is the interpreter hand-off in section 2.
- **The child after a fork:** the gate keeps no deadline state, and waiters don't exist in the child. `Instant` stays valid across fork for the forking thread's own call. `reset_in_child` overwrites the counts as today, and `deferred_releases` and the flag are handled per "Closing".
- **GIL and free-threaded:** every wait still goes through the blocker, so "no attached waits" holds, and the deadline check inside it only touches atomics and the clock. `begin_fork` runs detached, and CPython's import lock and stop-the-world come after the hooks, so a stuck fork holds neither. A waiter is detached, so a stop-the-world isn't held up. Under `crate::detach` the wait runs directly, as today. Both builds reproduced the wedge and both get the tests.
- **The module's rules:** the outermost-lock rule is unchanged, since a level holder never waits here; the slot and write lock taken before the gate stay in that order, and now always come back. Fork-safety stays atomics and thread-locals only: `deferred_releases`, the `deferred` flag and `fork_timeouts` are atomics, and no mutex is added.
- **The shuttle model:** `enter_shared_until` is built from primitives the shim already covers. See section 7.

### 7. Tests

Every stuck-fork test has a watchdog (`recv_timeout` in Rust, a subprocess timeout in Python): the last wedge hung `cargo test` for 28 minutes. Rust tests that use the global gate keep a fork stuck for under 1 s, because the whole binary shares it and `T` is 5 s.

**"Raises within the bound" can't catch a wait that skips the blocker.** A bounded wait that stays attached stalls the interpreter for the whole `timeout` and then raises the expected `Timeout`, so every such test passes on the wrong build, and shuttle has no blocker to check. Measured at `timeout=0.5` on 3.14 (`ticks.py`): worst ticker gap 501 ms on an attached build against 1 ms on a correct one. So a GIL-build test (skipped where the GIL is off) runs a ticker thread sleeping 1 ms and asserts a small maximum gap during the wait, for an attached caller (`get`) and detached ones (`Cache(...)`, `clear`). This is the only test of the "no attached waits" rule on this path.

**Regression from the repro.**
- `gate.rs`, deterministic, on its own `test_gate!`: a holder waits on a channel for an entrant; a fork claims; `enter_shared_until(now + 200 ms)` returns `None` no earlier than 200 ms; the entrant reports; the holder drops; the fork completes; a fresh entry succeeds; `holders()` and `parked()` are 0. Run with the old unbounded entry, this hangs, and the watchdog reports it.
- `store.rs`: the repro's shape on a real store. A `read` operation stays inside its closure; a fork claims; another thread's `store.contains(short, ...)` gets `Err(Timeout)`, which the closure receives; the operation returns; the fork reaches quiescence only then; a fresh operation succeeds. Delete the scratch repro.

**Each table row.**
- Rust: rows 1 to 3 (open, join and lookup time out; a successful one holds one level through the insert; `Cache(dir, size_limit=...)`'s shape directly, where `join`'s write times out and its failure path adds no wait, with the handle released at the drain); row 4 (a timed-out reopen leaves `closed` set and the count unchanged); row 5 (returns within 1 s at any `timeout`, the store stays in the registry when its last `Arc` is gone, the drain releases it and closes the pin, fd count checked, and the race with a fork ending at that moment; at `timeout=0`, a `del` during an ordinary fork waits it out and defers nothing); rows 6 and 7 (a read behind a fork times out on both paths; `get_with` stays within one `timeout` across two forks and a missing file); row 8 (a write, and a spilled write with its file discarded and the write lock free after); row 9 (`checkpoint` returns `Ok(false)` within its timeout; `maybe_checkpoint` doesn't wait; `fork_pending` counts, `busy` doesn't, and no backoff starts); row 10 (`Skipped`, immediately); row 11 (a non-owner on a store with a hold gets a deadline, and the owner is unaffected); row 12 (no false `NestedTransaction`; a real one still reported); row 13; row 14 (by assertion); row 15's existing tests unchanged, plus one pinning that the owner's-fork wait stays, and one on its own gate for the `Inherited` arm: a share made inherited with `end_child`, then `share.enter()` behind that "child's" stuck fork waits, under a watchdog, until the fork ends; rows 16 and 17 (dropped with a fork pending: nothing closed, the pool's idle connections closed by that fork, and fd count checked).
- Python, `tests/unit/fork_timeout_scenarios.py` run in subprocesses, as `fork_scenarios.py` is, since the gate is process-wide, on all four interpreters. The harness holds a transaction on a private cache, starts a thread in `os.fork()`, and polls `_gate_stats()` until `pending`. Then every public call on another thread, on a handle with `timeout=0.2`: `get`, `get(expire_time=True)`, `[]`, `in`, `len`, `volume`, `set` (small and spilled), `add`, `delete`, `del`, `pop`, `touch`, `incr`, `decr`, `next(iterkeys())`, `expire`, `clear`, `cull` (with a per-call `timeout=`), and `Cache(...)` on a new directory and on an open one, and a closed handle reused: each raises `Timeout` within the bound. `_hold_write_transaction().__enter__` (row 13) raises too. `checkpoint()` returns False. `close()` and `del cache` return within their budgets, on a small WAL and on a large one (the detached path through `crate::detach` and `try_attach`). Also `timeout=0` (immediate), nested calls inside the hold (never time out), and an iterator resuming at the right key after one. After releasing: the fork completes, the child reads and writes, nothing was lost, `holders` and `parked` are 0, and `fork_timeouts` matches the count.
- **`ensure_open`'s swap-to-load:** a stress test with N threads reopening and closing one handle, ending with the right handle count and D-018 clean.
- **`Caches`:** on a small `timeout`, a first lookup behind a fork returns a `NullCache`, logs at `WARNING` with no traceback, calls `on_open_error`, and opens the real cache after `min(retry_open_after, 1 s)`, with the fake clock the existing tests use. A real open failure still waits `retry_open_after` and logs at `ERROR`.

**The pickle-hook route.** Run these against the unfixed build first and record the hang, as above; then prove the fix. All on an inline pickled value:
- the hook waits for another thread's `get`, `set`, `close()` and `Cache(...)`: each returns as the table says, the hook's level goes, and the fork completes;
- the hook re-raises the `Timeout` it received: the caller's `get` raises `Timeout` under `'raise'` and `'miss'`, with the row kept (the existing passthrough);
- the hook swallows it and returns: the caller gets its value;
- the hook retries forever, until the test stops it: each call stays bounded and the fork stays stuck until then, which pins "clears only if the holder reacts";
- a spilled value's hook: no wedge, since no level is held. The control;
- the hook uses `ThreadPoolExecutor.submit` and `logging.getLogger` with the default import order: both return. This pins the hook-order fix, in a fresh subprocess;
- with `concurrent.futures.thread` unimportable: the package imports, the hooks are registered, and a fork still quiesces. Also in a fresh subprocess;
- a thread holding `logging._lock` calling the cache behind a fork: `Timeout` at its bound, and the fork then completes (the inversion in section 4).

**The shuttle model (`gate/model.rs`).** Add `enter_shared_until` and move existing models to `enter_shared_blocking`:
- *The cycle:* a holder waits on a channel for an entrant, plus a forker. Must finish; a regression trips the `LIVELOCK` assert, as `begin_fork`'s poll keeps advancing the logical clock. `Reach`: timed out with a fork pending.
- *The race at the deadline:* a forker with no stuck holder and a deadline a few polls away. `Reach`: both entered-after-waiting and timed-out. `Witness` proves no entry runs beside an exclusive fork; `assert_idle` proves nothing leaked.
- *Never times out:* a past deadline with own depth, with a held share, and on the forking thread: always `Some`.
- *Parked:* two forkers holding levels plus a bounded entrant: parked balances, and nothing deadlocks.
- *Composition:* model 7 with a bounded entrant that also times out holding a slot and the write lock: `in_use` and `waiters` return to 0.
- *The deferred drain:* the flag and its order live in `gate.rs`, with the count passed in as a closure, so they're modelled here. A deferrer and **two forkers**, each running the drain at its end, and the model asserts the count is 0 at the end. A single store-then-load pair passes on the wrong order, so one forker isn't enough; the `Reach` is a drain whose try failed because the second fork had claimed.

Only `gate` and `genlock` are modelled, so rows 11 and 12's "`None` proves it isn't ours" gets a gate-level assertion (a thread with a level or a held share always gets `Some`), and the store tests above. Shuttle has no blocker, so it says nothing about attached waits; that's the ticker test's job.

**The soak (step 3).** Stays one 24-hour run on the final commit, which now includes this.
- **Normal lanes:** `fork_timeouts` is reported, and any non-zero value is a problem. With timeouts of 30 and 60 s, even a fork behind busy threads and a forced checkpoint is far inside the bound, so it means a fork was stuck. `fork_pending` keeps `busy` meaningful as a count of WAL contention.
- **A stuck-fork lane:** one thread per worker, one at a time (as `held_lock` does), on a private directory with a `timeout=0.2` handle, every few minutes: hold a transaction, or sit in a pickle hook, while a thread forks and a helper's call gets `Timeout`; release once it does; the fork completes and the child reads and writes. Keeps each worker's fork stuck well under a second, so `STALL_AFTER` still means what it means. Runs alongside SIGUSR1 forks, for concurrent claims. New counters in the status and report, failing the run if they stay zero, as with `pop`.
- **Forks under load:** a short bench, not the soak: time `os.fork()` beside 8 threads looping on `timeout=0` calls that raise, on both builds, against the control of a fork beside 8 threads in a pure-Python spin with no cache. Without the control, the interpreter hand-off looks like the library's cost. Report the share of calls that got `Timeout`.

### 8. Docs

**`gate.rs`.** A new rule, after the two main ones, and the fixes it implies:

- New rule: "A wait behind a pending fork belongs to an operation and ends at its deadline. Only a thread with no level ever waits here, and a waiter holds no level and no count, so giving up leaves nothing to undo. Code with no deadline doesn't wait: it tries once and defers or skips. The exceptions are [`Gate::begin_fork`], since CPython can't refuse a fork and proceeding without quiescence would hand the child half a transaction, and [`HeldShare::enter`], while the share's owner is forking or parked or the share was inherited; `transact()` must bound those before it ships."
- The `begin_fork` doc states what a stuck fork does to other threads.
- **`gate.rs:469-470`** says this wait "can't deadlock". It can, per "The waits left": say what's true, that that fork doesn't wait for the share, and that a third thread's level can still form a cycle.
- **The comment at `gate.rs:1025`,** per the follow-up below, becomes: "Uses the global gate and HELD_FORK. A fresh thread starts with no guard and no levels of its own. The gate is process-wide, though, so both `begin_fork`s still wait for other tests' levels."

**Elsewhere in the code:**
- `store.rs` module docs: "No blocking wait happens while the gate is held" stays; add that the gate wait itself is bounded by the same deadline.
- `store.rs:1259-1261` ("Neither waits for the other"): correct it to match `store.rs:1256`'s entry in "The waits left".
- `ops.rs`: the `delete_version_nowait` doc ("Like any operation, it waits while a fork is pending") is wrong after row 10. The comment at `ops.rs:818` and `end_mid_operation`'s comment no longer describe a wedge: update them.
- `lib.rs`: `_before_fork`'s doc, the `Timeout` docstring at `lib.rs:24`, and a comment in `register_fork_hooks` saying why those imports come first.
- `error.rs`: `Timeout`'s doc. `wal.rs`: `record_outcome`'s doc ("Anything but success starts the backoff") and the new outcome. `_core.pyi`: the timeout docstrings.

**`user_guide/concurrency-and-forks.qmd`.** A new subsection under Forks, "How long a call waits for a fork":

- A fork counts against `timeout`, so `timeout` must exceed in-flight work plus the fork plus the interpreter hand-off. A fork takes about a millisecond in a quiet small process, but tens to hundreds of milliseconds beside busy threads on a GIL build, longer with a large process, and it waits for work in flight such as a forced checkpoint. A `timeout` under that sees `Timeout` during ordinary forks, and a loop on `timeout=0` adds to the problem.
- The stuck case, plainly: code that holds a transaction or runs in a pickle hook and waits for another thread's cache call can keep a fork from finishing. That other thread's call now raises `Timeout`, and the fork finishes only once your code lets go. Meanwhile `os.fork()` doesn't return, and every `Cache` in the process times out. A hook stuck in C code holding the GIL stops everything.
- `checkpoint()` returning False, and `close()` taking at most about 2 s and returning while a release is deferred.
- Other libraries' before-fork hooks: ours runs before `logging`'s and `concurrent.futures`', so they stay usable while a fork waits, but a hook that takes a lock and is registered after `diskcache_oxide` stays held. And a thread that calls the cache while holding `logging`'s module lock can hold up a fork until its `timeout`.

`user_guide/caches.qmd`: a `Timeout` at open is remembered for at most 1 s and logged as a warning (section 5). Rebuild `site/`.

**`tests/DIVERGENCES.md`: D-027 "A pending fork counts against `timeout`".** Kind: note, pinned by `test_fork_timeout.py`.
- diskcache has no fork barrier, so its calls never wait for a fork.
- oxide: what a call does, what `close`, `del` and `checkpoint` do, and that `os.fork()` can still wait forever, and why.
- Also: a line in D-012 pointing at it, in D-018 for the deferred release, and in D-024 for `checkpoint`.

### Unknowns

- How long a fork takes at Talk Python's RSS. `bench.fork_cost` on a production-sized process decides what `timeout` is sensible under `durable=True`.
- The real overshoot on a loaded GIL build. Set the test bounds from a measurement, not a guess, or they'll flake.
- Whether a Rust test using a short timeout now flakes when it runs beside another test's global fork. The ones found (`ops.rs:822`, `store.rs:2015`) assert a timeout anyway, but a success under a short budget elsewhere would flake. Run `cargo test` repeatedly.
- Which third-party libraries have before-hooks that take a lock and are registered after ours. They stay held while a fork is stuck, and that can't be fixed from here, so the guide warns about it, but how common they are hasn't been surveyed.
- A daemon thread in the slow path at interpreter shutdown: the blocker re-attaches as it does today, and this step doesn't change it, but it hasn't been tried with a stuck fork.

### Build order

1. Record the "before" of the tests above on the current build, including `bench.gate`.
2. The gate API (the bounded entry and the deferred drain) with the models.
3. The call sites and core tests.
4. The Python layer: `ensure_open`, `close`, the ticker test and the rest of the Python tests.
5. The hook order. **It must not land before the bounded entry,** because of the inversion in section 4.
6. `Caches` and the docs.
7. `bench.gate` after, the fork bench with its control, the suite on all four interpreters, and `scripts/check.sh`.

The architect reviews it like every other step. Commit before step 2 starts.

## Step 2: `Cache(..., durable=False)`

Michael, 2026-10-30: "durable means DURABLE as much as we can deliver." An app whose every caller falls back to its source of truth wants a cache that can't take a request down. So this is one option covering every operational failure, not only timeouts (this replaces the `handle_timeouts` draft). Talk Python's `DurableCache` hides read errors today, and its `set` raises.

**The option.** `durable: bool = False`. Per handle, not persisted. `NullCache` accepts and ignores it; `Caches` passes it through like any setting. Off by default, so diskcache's behavior is unchanged.

With `durable=True`:
- `on_decode_error` defaults to `'miss'` instead of `'raise'`. An explicit value wins.
- An operational failure on a single-key call doesn't raise: `Timeout`, `OSError` (disk full, I/O), SQLite errors including a damaged database, and the `RuntimeError`s of a child forked mid-transaction that can't write.

| Call | On an operational failure |
|---|---|
| `get`, `pop`, `in` | the default, or False. `cache[key]` raises `KeyError` |
| `set`, `add`, `touch` | False. `cache[key] = v` drops the write |
| `incr`, `decr` | `default ± delta`, as `NullCache` does, so a rate limiter fails open. `default=None` raises `KeyError` |
| `delete` | False. `del cache[key]` raises `KeyError` |
| `clear`, `expire`, `cull`, `checkpoint`, `len`, `volume`, iteration | raise as today: admin callers need the truth, and these have their own `timeout=` |
| anything inside a held transaction | raises: swallowing it would break the transaction's atomicity |

Still raised, because they're bugs and not outages: `TypeError` (a non-`str` key, a value that can't be stored without pickle), `ValueError`, `UnsafeValueError`, a non-numeric `incr`, `NestedTransaction` and the other misuse errors. `MemoryError` and `KeyboardInterrupt` always raise.

**`delete` (Michael, 2026-10-30): returns False.** A failure never reaches the caller. A swallowed `delete` looks like "wasn't there", so that invalidation is lost and stale data is served until the entry expires. The log line is the signal, and the docs must say this plainly.

**Reporting.** A swallowed failure is logged as a warning on the `diskcache_oxide` logger, at most once per handle every 60 s, with a count of those suppressed since. An outage can be thousands a second.

**Tests:** every row of the table against a peer process holding the lock, and against a real I/O failure where one can be forced; the default raises; a held transaction raises; bugs still raise; the log limit, with a count; `Caches` passthrough; `NullCache` accepts and validates it (`bool` only).

## Step 3: re-soak

After steps 1 and 2 and the 24-hour result: add `pop` to the soak worker, with a check that each value is delivered exactly once, then stop the current run, keeping its partial results, and run 24 hours on the final commit on the mini (Michael, 2026-10-30). Phase 3's production cutover waits for it.

## Architect's review of this plan (2026-10-30): changes to make while building

Exactly-once holds: the compare-and-delete runs inside `BEGIN IMMEDIATE`, and `store_seq` never repeats. The design stands, with these changes. Nothing below is built yet.

**Step 1 (`pop`):**
- `decode_failed` can't be reused as written: it returns before classifying under `'raise'`, and it uses the non-blocking delete. Add `Row::Popping(version)` or a `pop_failed`, with the blocking delete under both policies, and define all three outcomes. `true`: raise, or return the default and report. `false`: go back to the read. `Err`: under `'raise'` it replaces the decode error, with that error as its `__cause__`; under `'miss'` it raises unless durable.
- Call `store.check_writable()` at the top of `pop`. Without it a D-020 child gets a miss where it raises today.
- One `Instant` deadline for the whole `pop`, not a full timeout per attempt (otherwise about 9× the budget). Keep one clock read across retries.
- Use `spill::OPTIMISTIC_ATTEMPTS`. The fallback isn't rare on a hot key, because the lost-race window includes the write-lock queue; say so in the docs.
- `tests/DIVERGENCES.md` entry, since the defaults change: a hook sees the key still present, a raising hook can keep the row, `pop` of a missing key no longer times out behind a writer, and a hook that pops its own key now recurses to `RecursionError`.
- A row whose value file is missing can stay after `pop` under contention, as with `get`. Accept, document, test.
- `test_decode_errors.py:167-169` flips too, not only the test the plan names.

**Step 2 (`durable`):**
- Classify with `Error::is_operational()` in `error.rs`, an exhaustive match with no wildcard. Operational: `Timeout`, `Io`, `DiskFull`, `SqliteIo`, `Sqlite` except `OutOfMemory`, `Unwritable`, `ForkPoisoned`. Raised: `TransactionAborted`, `KeyMissing`, `NotNumeric`, `Overflow`, and the hold-misuse errors. Map `SQLITE_NOMEM` to `MemoryError`, which falls through to `OSError` today.
- Swallow only at the store seam (`.map_err(to_py_err)`, one helper), so a `Timeout` raised by user code isn't taken for this cache's failure.
- **Decide:** `classify` still passes `OSError`, `Timeout` and `RecursionError` from a pickle hook out of a durable `get`. Recommendation: under durable they're a miss with the row kept, reported once per version. `MemoryError`, `BaseException` and `UnsafeValueError` still raise.
- Add to the table as "raises": the constructor (use `Caches`) and starting a hold.
- A durable `pop` whose delete fails returns the default, never the decoded value, which could otherwise be delivered twice. Step 1's table needs a durable column.
- The hold rule means "a hold on this store": add `Store::held_by_this_thread()`, on the error path only. `GATE.own_depth()` is wrong here, since it's process-wide and true inside any hook.
- `on_decode_error` becomes `Option<DecodePolicy>` with default `None`, in the signature, stubs and `NullCache`.
- **Reporting:** two log buckets, busy (`Timeout`, warning) and broken (the rest, error), so a timeout storm can't hide a damaged database. Add a third for swallowed `delete`, per-op counts in the message, and a stats accessor like `_wal_stats`, so "N invalidations lost" is visible. Atomics and compare-and-swap, the first failure logged at once, and time taken from the operation's `now` so tests can drive it.
- **Tests:** a peer holding the lock can't make `get` or `in` fail. Injections that work: `max_connections=1` with another thread in a slow hook; `chmod 0` on a value file (not as root); `RLIMIT_FSIZE` with `SIGXFSZ` ignored, in a forked child; fork inside a hold.
- **Docs, plainly:** a dropped `set`, `pop` or version-bump `incr` serves stale data just as a dropped `delete` does, and "until it expires" means forever with no expiry. A fake `incr` is a duplicate if used as an id. `add` is False under durable but True on `NullCache`, so claims fail closed: use a non-durable handle for them. Durable doesn't bound time, so pair it with a small `timeout=`, though one that still exceeds in-flight work plus a fork plus the interpreter hand-off, or forks give misses (step 1b, section 2). In database vocabulary "durable" means "survives a crash", and this option can drop writes, so the docs need a sentence saying what it means here.

**Step 3 (soak):** confirmed, the kit has no `pop`. Add:
- an exactly-once lane on the ledger: a producer's winning `add('pq:<slot>', token)`, consumers `pop` and then `add('delivered:<token>', worker)`, where a False is a duplicate, plus a final check in `maint` with an allowance for kills;
- `chaos.pop` in `one_op`; `pop` for half of `held_op`'s deletes; `pop` in `fork_test`;
- a durable lane: after `set` returns False the read-back is still the previous version, and no operational exception escapes;
- new counters in the status and report, and fail the run if the `pop` count is zero.

Per Michael (2026-10-30), the re-soak is a fresh full 24 hours on the final library commit, not 6-8 hours, replacing the current run once this plan is built.

## Progress

**Step 1 built (2026-10-30), with the architect's first review's changes made. Not yet committed.**

- **Core:** `Store::delete_version(timeout, version) -> Result<bool>`, through `write_files`, so a held transaction defers the file's unlink. `Store::pop` and `Pop::Refused` stay for the fallback.
- **`Cache::pop`:** `check_writable()` first, then `pop_value`: `read_row` (the `get_with` call `lookup` now shares), then `delete_version`, up to `spill::OPTIMISTIC_ATTEMPTS`, then the locked `Store::pop`. One `Instant` deadline, and the one clock reading from the top of the call. `pop_error` reports the cache's timeout, since the store only knows what was left.
- **`pop_failed`** rather than a `Row::Popping`. Foreign and passthrough keep the row and go through `decode_failed` with `Row::Stored`. Unreadable takes the blocking delete under both policies. `true`: raise, or return the default and report. `false`: read again. `Err`: raised with the decode error as its `__cause__`, under both policies. Step 2 adds the durable case there.
- **One addition to the review's list:** a race lost once the deadline has passed raises `Timeout`. This `pop` deletes nothing, and whatever the other writer left stays. It doesn't retry, or a slow hook that keeps overwriting its key would run 8 times whatever the timeout. It doesn't take the fallback either: that first draft destroyed a foreign value after one lost race (the architect reproduced it with `timeout=0.05` and a slower hook). So the fallback only ever follows 8 real losses.
- **`Row::Removed` now means two things in the message:** "deleted" for an unreadable value, and a sentence about the fallback for a foreign one, the only way one can still go.
- **Tests:** `tests/unit/test_pop.py` (new); exactly-once races for threads (`test_threads.py`, both layouts) and spawned processes (`test_processes.py`), on every backend, each with a many-keys race and a producer with separate consumers (the first draft's `set`-then-`pop` hot key couldn't see a double delivery, and on a GIL build only the processes version can); three Rust tests in `ops.rs`; `pop` in the D-020 fork scenario; `test_decode_errors.py:167-169` and `test_pop_of_a_foreign_tag_says_it_was_kept` flipped; `test_pop_decodes_after_its_write_commits` renamed `test_pop_decodes_outside_any_write_transaction`.
- **Docs:** D-026, the guide's table and limits, the `_core.pyi` docstrings, `site/`.
- **Measured:** `pop` of a small value (a two-key dict; 20,000 keys, p50, three old/new rounds interleaved, on battery with other load, so treat small differences with care). A hit went from 16.1-16.9 us to 17.2 us on 3.14 and 16.5-18.8 us to 17.4-17.6 us on 3.14t, leaving out one slow outlier round on each side, so about 1 us, or 5%. A miss halved, from 1.8 us to 0.9 us, since it no longer takes the write lock. `get` of a hit is unchanged at 1.2 us. The "before" was measured on the unedited build (16.46 us on 3.14, 17.00 us on 3.14t).

**Open follow-ups:**

- `read_row(left())` isn't bounded by that one `left()`: `get_with` (`ops.rs:158-210`) gives each of its internal reads and its D-016 write the full value it was handed, so pool exhaustion plus value-file churn could overshoot the deadline. Reasoned, not reproduced, and `get` has the same shape.
- The soak's `pop` lane (step 3) should count fallback hits. Only hooks exercise the fallback deterministically, and how often it runs on a heavily overwritten key hasn't been measured.
- **Decided (Michael, 2026-10-30), to be designed next:** waiting on a pending fork will honor the operation's `timeout`. It gets its own reviewed step, after the `pop` commit and before `durable=True`. The finding: a thread holding a gate level (a hold, or a pickle hook on an inline value) that depends on another thread's cache call wedges the gate permanently if a fork is pending, because `Gate::enter_shared` and `begin_fork` aren't bounded by any `timeout`. The other thread waits for the fork, the fork waits for the level, and the level waits for the other thread. Reproduced at the Rust level through a cross-thread hold, where it hung `cargo test` for 28 minutes (`end_mid_operation` entered the gate plainly on the thread that owned the hold). The pickle-hook route is reasoned from the code, not reproduced. The test helper is fixed; `gate.rs` isn't, on purpose.
- **With the gate step:** the comment on `a_leftover_fork_guard_is_ended_by_the_next_before_hook` (`gate.rs:1025`) says a fresh thread means "no other test's levels count". That's wrong, since the gate is process-wide. Left alone so this commit doesn't touch `gate.rs`.
