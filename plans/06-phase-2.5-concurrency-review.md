# Phase 2.5: Concurrency deep review and soak

The roadmap's Phase 2.5 section lists the goals: independent deep reviews of the concurrency core, model-checker tests, a 24-hour soak on macOS and a shorter one on Linux. It also carries ten follow-ups, nine from the architect's Phase 2 review and one found by the soak. This plan orders the work into small steps. Each step gets the architect's review before it's built, and the phase gets one review at the end.

## Done before this plan

- **The soak kit** (`soak/`: worker, orchestrator, report, `run.sh`, `package.sh`):
  - many processes × threads, alternating 3.14 and 3.14t on the same caches;
  - SIGKILL and respawn, forks under load, handle reopens, `clear`/`expire`/`cull` under load;
  - checks every minute: checksummed self-describing values, ledger counters and single-writer keys, `add()` races, `totals`, missing files, `integrity_check`, stalls (with native and Python stacks), and RSS/fd trends with leak fits;
  - it saves CSV and JSON data plus a self-refreshing `report.html`.

  Its detectors were tested by planting a stall, and four bugs in the kit itself were fixed before shipping.
- **The first 24-hour run** started on the Mac mini (2026-10-28 17:30). At 35 minutes: 0 problems; memory flat after warm-up; the chaos WAL at 300 MiB against a 21 MiB database.

## Step 1: built-in WAL management (roadmap item 10)

**Problem:** SQLite's automatic checkpoint is passive. It copies WAL frames into the database but never waits, and the WAL can only reset, so that new frames go back to its start, when no reader still uses the old frames. With readers active every moment, which is a read-heavy cache's normal state, it never resets, so the WAL file grows without limit. SQLite also never shrinks it. The soak shows 176 → 300 MiB in 30 minutes.

**Design, as built** (the architect reviewed the plan, and then the implementation; see the Progress section):
1. **`wal_size_limit`:** a `Cache(..., wal_size_limit=bytes)` option, default 64 MiB (Michael's decision), at least 1 MiB. Per process, not persisted; the largest value any handle asks for wins, like `sqlite_cache_size`. `NullCache` accepts and ignores it, and it's in the stubs.
2. **Connection settings** applied at lease time (they write nothing, so D-019 holds):
   - `journal_size_limit` = limit / 2, so a normally reset WAL shrinks back, and never looks over the limit;
   - `wal_autocheckpoint` scaled to a quarter of the limit in frames, at most SQLite's 1,000, so a healthy WAL stays far under the trigger.
3. **A forced checkpoint when the WAL is over the limit.** It runs after `Store::write`'s retry loop returns, still holding the in-process write lock, so a checkpoint problem can never replay a committed write. It runs only for top-level writes: never at gate depth > 0, inside a held transaction, or through `write_nowait`.
   - At most every 250 ms or every 64 writes, `stat` the `-wal` file;
   - if it's strictly over the limit, run one `PRAGMA wal_checkpoint(TRUNCATE)` in its own slot and lease, under a standard 2 ms attached window. It waits out in-flight readers, copies the remaining frames and truncates the file.
   - Busy comes back as a row (`busy = 1`), not an error, and is followed by a 1 s backoff. No backoff after a success.
4. **Counters:** `Store::wal_stats()` / `Cache._wal_stats()` report checks, attempts, successes, busy, errors, `no_slot`, `max_pause_us` and `last_wal_bytes`. The soak kit records them for the next run.
5. **Later:** when the background maintenance thread lands (the expiry sweeper, vision.md), the forced checkpoint moves there.

**Error handling:** a failed or busy checkpoint never fails the write that triggered it. The write has already committed. Busy means "try later", and any other error is logged into the store's counters, not raised.

**Tests:**
- **A regression test that must fail before the fix.** Constant reader threads, in this process and in a spawned process, plus a writer adding about 10 KiB values for about 15 s, with `wal_size_limit` = 4 MiB. The WAL file must stay under 2× the limit at every sample, and end there.
- **The trigger is rate-limited.** A burst of writes runs at most one forced checkpoint per second.
- **A busy checkpoint doesn't fail the write.** A peer process holds a read transaction open (SqlitePeer `BEGIN` plus a read) while writes push the WAL past the limit: the writes succeed and the WAL stays big. After the peer releases, the WAL drops to the limit within a few writes.
- **Opening still writes nothing (D-019),** with `journal_size_limit` set.
- **The gate:** `set_small` must not regress. Measured +0.9% over 10 interleaved rounds; gate GO at 0.79×. The benchmark row for WAL size and pause under the 99:1 mix is deferred to step 6. The soak records the pause meanwhile.

**Docs:** a D-024 entry if this behavior differs from diskcache's (diskcache has no WAL limit), design doc section 7, and the README.

## Later steps (ordered; each gets its own short design note here when it starts)

2. Waiter counters leak on unwind: RAII guards (roadmap item 1).
3. Measure, then fix, the interpreter re-attach on already-detached threads (item 2).
4. `pop` decodes after `COMMIT` (item 7).
5. Items 3-6, 8 and 9 from the roadmap list.
6. Graceful-restart chaos (SIGTERM, clean shutdown, respawn) in the soak, for the next run.
7. Model-checker tests (`loom` or `shuttle`) for the gate and the write lock.
8. Michael's independent reviews (Claude, Fable, Astra): merge, triage, fix.
9. The Linux soak.

## Exit criteria

As in the roadmap: every review finding resolved or explicitly accepted, a clean 24-hour soak on macOS and a clean multi-hour soak on Linux, `scripts/check.sh` and `scripts/linux-check.sh` green, and the gates GO.

## Progress

- **Step 1, built-in WAL management (2026-10-28).**
  - **Diagnosis:** the soak's WAL grew in steps that lined up with `clear()` calls, not steadily. Steady reads alone didn't reproduce it, in-process or across processes. What did: readers in other processes plus write bursts (`wal_burst2`), which left 538 MiB-1.09 GiB WALs on 3.14t, permanently. On 3.14 the GIL leaves gaps for normal resets (4.3 MiB). Free-threaded builds with readers truly in parallel are what need the forced path.
  - **Review:** the architect reviewed the plan and changed four details: the 2 ms window, busy read from the row, the check after the retry loop, and thresholds scaled from the limit; plus TRUNCATE and counters. The implementation review found one must-fix: the depth > 0 skip tested the token, which `try_lock` usually grants, instead of the depth. Fixed, with `store::nested_and_nowait_writes_never_check_the_wal`, which fails with the old check.
  - **Result:** in the same reproduction the WAL tops out at 2-32 MiB (the half-limit truncation point), and the bursts take the same time. `set_small` +0.9% over 10 interleaved rounds on a quiet machine; an earlier 17% reading was machine load. Gate GO on 3.14 at 0.79×. Soak-kit trial: worst forced-checkpoint pause 7 µs.
  - **Tests:**
    - core: `wal::*` (4), `nested_and_nowait_writes_never_check_the_wal`, `a_larger_wal_limit_from_a_second_handle_is_applied_to_pooled_connections`;
    - `tests/unit/test_wal.py` (12):
      - the burst regression with reader processes, which bites on 3.14t;
      - a healthy workload never forces a checkpoint;
      - a busy checkpoint never fails the write (errors stay 0, attempts are bounded by elapsed time), and the WAL recovers after;
      - held transactions never checkpoint;
      - validation, and `NullCache`.
    - The peer helper gained `begin READ`, a held read snapshot.
  - **The Linux run found a second problem:** every forced checkpoint reported busy. A single `TRUNCATE` spent its 2 ms window copying a backlog of hundreds of MB, and had nothing left to wait for readers. The fix is two steps, `PASSIVE` (no window) and then `TRUNCATE` (a fresh 2 ms window). Results from `python -m bench.wal_burst` (4 reader processes × 4 threads, 3 bursts of 20k sets plus `clear()`, 3.14t):

    | | old behavior (limit off) | one step, 64 MiB | two steps, 64 MiB | two steps, 4 MiB |
    |---|---|---|---|---|
    | Linux peak → final | 527 → 527 MiB | 537 → 32 MiB, 0/23 succeeded | 185 → 32 MiB, 9/22, pause ≤ 23 ms | 382 → 2 MiB, 6/29, ≤ 26 ms |
    | macOS peak → final | 538 → 538 MiB | 32 → 32 MiB | 122 → 18.5 MiB, 18/19, ≤ 4.3 ms | 5.0 → 2.0 MiB, 311/311, ≤ 1.6 ms |

    So the permanent bloat is fixed on both platforms. On Linux, heavy cross-process reading still allows a temporary overshoot during a burst. Michael (2026-10-28): fine under crazy load, as long as it's tamed eventually. Recorded as a D-024 limitation; the background maintenance thread is the way to do better.
  - **Deferred:** the benchmark row for WAL size and pause under the 99:1 mix (to step 6). `bench/wal_burst.py` covers the burst case meanwhile. The soak kit now records checkpoint counters and charts them for the next run.
- **Checkpoint pauses in the soak (2026-10-28).** The 24-hour run on the mini (commit 5e91b60) showed forced-checkpoint pauses of 245 ms to 4.1 s, mostly in 3.14 GIL workers, where the whole process waits with the triggering thread.
  - **What causes them:** the checkpoint's own work is cheap even under the soak's load (PASSIVE 5 ms, TRUNCATE 1.4 ms on an 89 MiB WAL, timed on the mini). The long pauses are the mini's disk stalls: the baseline run (Phase 2, no forced checkpoints) already showed `set` p99 up to 3.46 s. Read tails did grow on the mini (`get` p99 max 0.4 → 8.5 ms) but not on the laptop (0.5 ms).
  - **Change:** the checkpoint now runs with the interpreter released (`gate::run_blocking`), even though the thread holds the gate, so a GIL build's other threads keep running. The architect found no deadlock and no must-fix. The safety argument rests on a rule now written into the gate docs: no thread waits on a gate holder while attached; every wait for the write lock, a slot or a pending fork goes through the blocker. Also done:
    - the lease stays alive across the blocker, so a panic can't leak a pooled connection;
    - `enter_shared_blocking` is test-only;
    - split counters: `max_passive_us`, `max_truncate_us`, and `slow` (≥ 100 ms), recorded by the soak kit.
  - **RESTART vs TRUNCATE A/B** (measuring the write that triggers the checkpoint and the first write after it): RESTART saves about 0.3 ms on the trigger but adds about 0.25 ms (max 8.7 ms) to the next ordinary write, which holds the GIL. A wash, so TRUNCATE stays.
  - **Laptop soak, 7 × 4 flat out, 10 min:** 1 slow checkpoint of 328 (111 ms, in TRUNCATE), `set` p99 ≤ 58 ms, `get` p99 ≤ 0.5 ms.
- **Reasonable-load test (Michael, 2026-10-28):** 4 processes × 2 threads, 1,000:1, a steady 100 ops/s, chaos every ~5 minutes, 10 minutes; the soak kit gained `--mix` and `--rate`.

  | | macOS laptop | Linux 3.14 | Linux 3.14t |
  |---|---|---|---|
  | `get` p50 / p99 | 39 / 56 µs | 26.5 / 34 µs | 37 / 49 µs |
  | `set` p50 / p99 | 1.0 / 1.6 ms | 0.43 / 4.8 ms | 0.79 / 2.7 ms |
  | RSS per worker | 17.5 MiB | 23-28 MiB | 30-41 MiB |
  | WAL peak, forced checkpoints | 1.3 MiB, 0 | 1.4 MiB, 0 | 1.4 MiB, 0 |

  All passed with 0 problems. At normal rates SQLite's own checkpointing keeps up, so the D-024 path costs nothing. The free-threaded memory premium mostly disappears without flat-out churn.

  **Why paced latency is 30× the benchmark:** cold CPU state between calls, not oxide. With busy-waiting between calls, a `get` takes 1.0 µs with no gap, 1.6 µs after 1 ms, 11 µs after 10 ms and 32 µs after 80 ms. A plain `dict` lookup degrades the same way (0.04 → 2.96 µs, 74×). Any library pays that on an occasional call; oxide's own work is the 1 µs hot figure.
- **Manual checkpoints by default (Michael, 2026-10-28).** The soak kept producing multi-second forced-checkpoint pauses on the saturated mini: 81 of 1,841 attempts were slow, the worst 4.3 s. Both steps stall (PASSIVE up to 3.7 s, TRUNCATE up to 3.2 s), because both write and `fsync` a disk that's saturated. Michael's call: no housekeeping on the write path unless the app asks for it.
  - Default `wal_checkpoint="manual"`: writes never checkpoint. `journal_size_limit` and the scaled autocheckpoint stay; they're free, and at normal load they keep the WAL at a few MiB.
  - `Cache.checkpoint()`: retries until the cache timeout, returns True/False, and never raises for busy.
  - `close(checkpoint=True)` (default): checkpoints a WAL past its trim size, waiting at most 1 s; `checkpoint=False` skips it. SQLite also removes the WAL when the last connection to the database closes.
  - `wal_checkpoint="auto"`: the previous behavior, as an opt-in.
  - The README's new "Shutting down" section recommends `checkpoint()` at shutdown, but doesn't require it.
  - The soak gained a `checkpoint` chaos action, the cookbook pattern.
  - **Later, if users hit it:** a docs cookbook recipe, a thread that watches the `-wal` size and calls `checkpoint()`. The real fix for write-path stalls under saturated disks is still the background maintenance thread, which would also take SQLite's own autocheckpoint (and its `fsync`) off the write path.
  - **Normal-load test (Michael's spec, 10,000 reads and 100 writes a minute, 4 × 2 workers, 10 minutes, laptop, every call timed):** PASS.
    - `get` p50 35 µs, p99 88 µs, max 5.8 ms;
    - `set` p50 0.53 ms, max 13.7 ms;
    - WAL peak 4 MiB, 0 forced checkpoints;
    - 22-38 MiB per worker.
  - **Architect review of the manual-checkpoint change:**
    - **Applied:**
      - `checkpoint()` at gate depth > 0 (the caller's own transaction) returns False without trying, and the store-wide `pinned_active` test is gone, so another thread's transaction is waited for;
      - `close()` checkpoints only for the process's last handle on the store, and not during the 1 s backoff after a busy attempt;
      - `close(*, checkpoint=True)` is keyword-only;
      - `checkpoint(timeout=None)`.
      - New tests: waiting for another thread's transaction, returning at once inside your own, and the timeout argument.
    - **Should-fixes, done (2026-10-28, night):**
      - `checkpoint()` retries only busy rounds (busy, locked, no free slot) and returns a real error (a full disk, an I/O error) at once;
      - one `checkpoint()` call is one attempt in the counters, with exactly one outcome (`attempts = successes + busy + errors + no_slot`); pause times and `slow` are per round; a success ends the backoff;
      - the docs say `timeout` bounds the waiting, not a round already under way. The TRUNCATE window is **not** capped at the deadline after all: the architect's re-review found that a window computed before PASSIVE was already used up after a long copy, the exact failure the two-step design fixes. Each round's TRUNCATE gets a fresh 2 ms window, so a round can end up to 2 ms past the deadline;
      - `close()`'s two doc comments merged; D-024 notes that `checkpoint()` raises in a D-020 child (also checked in `fork_scenarios.d020`) and that closing twice is harmless;
      - tests: one call is one attempt; closing twice (and `__exit__` after `close()`) is harmless and the cache reopens; closing one of two handles three times leaves the other as the last, and its close checkpoints.
- **Load framing (Michael, 2026-10-28):** half of what the Mac mini can do flat out, about 180k cache operations a second across 7 processes × 4 threads, with roughly 1,000 writes a second plus chaos, is the **absolute upper bound** for one cache directory. Beyond that, the answer is FanoutCache (6c). Normal load is about 100 requests a second at 1,000:1. Saturation runs exist to shake out races, leaks and deadlocks; their latencies aren't targets.
  - Half-load run, first minutes: `get` p50 4.7 µs and p99 32 µs, worst `get` per minute 0.3-1.3 ms; `set` p50 72 µs and p99 1.7 ms; worst `set` 8 ms; WAL 4-5 MiB. At full load the worst write was 2.2 s, with 3 of 10 minutes over 500 ms, from SQLite's own autocheckpoint `fsync` on the saturated disk.
- **Step 2, waiter counters on unwind (2026-10-28, night; roadmap item 1).** The write lock's `waiters` and `starving` counts are RAII guards (`Counted`, generation-tagged like `SlotPermit`, sharing its `release_one`), so a panic out of the blocker can't leave `starving` raised and make every `try_lock` fail. The release profile keeps `panic = "unwind"`, so the path was reachable. Tests: an unwind takes both counts back; a guard from an older generation releases nothing. Architect: no must-fixes; doc wording, the shared helper and a test comment applied. The gate's own `parked`/`holders` counts were checked too: their waits only sleep, so they can't unwind.
- **Step 3, the interpreter re-attach (2026-10-28, night; roadmap item 2).** Measured first with the new `bench.reattach` (`clear()` of 20k rows, chunks of 100, on 3.14 GIL): 31.5 ms a chunk with 2 writer and 2 reader threads, 15 ms with readers only (3.14t: 2.4 ms). Fixed with a thread-local "already detached" flag set by the py crate's `detach` helper around the five Rust-only detached calls; the blocker then waits without re-attaching. After: 2.2 and 0.28 ms a chunk; writers unchanged at about 24k sets/s. The gate docs state the rule (work run through the helper must never attach). GIL-only regression test: 1.08 s before, about 15 ms after. Architect: no must-fixes.
- **Step 4, `pop` decodes after `COMMIT` (2026-10-28, night; roadmap item 7).** `Store::pop` returns the value still encoded (`Popped`), and the Python layer decodes it after the commit. TEXT is copied raw as a BLOB (architect's catch: rusqlite's owned `Value` conversion checks UTF-8, which would have made `pop` and `get` disagree on a corrupt row). A value that fails to decode is still removed, as before and as upstream does. Tests: inline and spilled values whose `__setstate__` writes the same cache, and the same inside a held transaction.
- **Step 5, the remaining carry-overs (2026-10-28, night; roadmap items 3-6, 8, 9).**
  - **3, done:** a waiter that isn't starving sleeps while the lock is reserved for a starving one, instead of waking every 50 µs to a `try_lock` that refuses. Same outcomes, fewer wakeups, so no direct test.
  - **4, done:** at gate depth > 0 a write takes a free lock even while someone is starving (`try_lock_nested`), so it doesn't race the starving waiter inside SQLite.
  - **5, measured and not adopted:** routing `cull()` through `clear()`'s pause and priority re-take made it 3.5× slower on 3.14t against 4 tight writer threads (2.6 s vs 0.73 s for 98k rows) and only cut the writers' worst wait from about 24 to 15 ms; on the GIL build writes got in equally often either way. Starvation mode (added after this carry-over was written) already bounds both sides at about 20 ms. A liveness test checks that `cull()` finishes promptly against 4 writer threads. Architect agrees. **For Michael:** the same argument applies in part to `clear()`/`expire()`'s per-chunk yield. The architect would keep it (they have no natural bound, and the yield only runs while a writer waits) but measure the unconditional 100 µs pause and a shorter `MAX_YIELD` first.
  - **6, done:** `begin_hold` at gate depth > 0 never waits for the write lock: it takes it if free, or fails at once with `Error::WriteLockBusyInTransaction` (Python `Timeout`). Noted on roadmap 6b: `retry=True` must never retry it.
  - **8, done:** a file never has two live stores. Handles are registered (`claim`) and released under the registry mutex through one signed count (`count_handle`) whose landing point decides live or retired, so a Python handle's add and release may land in either order. A retired store that a closed handle still holds is revived, by a new open of the same directory (matched on the canonicalized path, since a retired store no longer pins its inode) or by the closed handle being used again, unless another live store already serves the file (a hard link): then it stays retired and works by path. Connections returned to a retired store are closed, not pooled (D-018). Architect, two rounds: a must-fix (a reused handle could revive beside a live store reached by another spelling) and a mutex-based close/reuse fix replaced by the signed count (a per-handle mutex waited on attached could deadlock behind a fork). Tests: revive instead of duplicate, 8 threads racing opens and closes, `find` only for its own directory, an aliased path, a hard link, a release before its add, a lease returned after the last close.
  - **9, done:** the pool's idle cap rises when a handle asks for more connections (`raise_max_idle`), as `cache_pages` does.
- **Step 6, graceful-restart chaos (2026-10-28, night).** A new `restart` chaos action does what gunicorn does: SIGTERM to a short-lived worker (one at least 30 s old, so its handler is installed), which must finish its calls, verify every key it owns, write exact final counts (held to the ledger at the end of the run), close its caches (with the checkpoint on close) and exit 0 within 90 s, then a fresh worker. A hang is a stall (stack captures, `restart-hang`); a bad exit or missing final stats is a problem. Counted in `restarts` (status line, timeline column, report chart and totals). Local 4-minute run: 7 restarts at 0.04-0.17 s each, PASS. First used by the Linux soak.
- **Step 7, model-checker tests: design note (2026-10-28, night; for architect review before code).**
  - **Tool: shuttle 0.9 (randomized and PCT schedule search), not loom.** The gate and the write lock wait in unbounded sleep-poll loops with time-based deadlines. Loom's exhaustive search needs every loop bounded and blows up on them; shuttle explores many random or PCT-prioritized schedules of the real code and finds the bugs that matter (lost wakeups, counter leaks, mutual-exclusion breaks, deadlocks it reports when every thread is blocked) at a cost that scales to the real state machines. A dev-dependency only, behind `cfg(shuttle)`, so normal builds and `cargo deny` for the shipped crates are unchanged.
  - **A shim, `crate::sync`:** re-exports `AtomicBool/AtomicIsize/AtomicU64/AtomicUsize`, `Arc`, `thread::{sleep, yield_now}`, `thread_local!` and a clock (`now()`) from std, or from shuttle under `cfg(shuttle)`. `gate.rs` and `genlock.rs` import from it instead of `std`. Under shuttle, `sleep` becomes a yield point and the clock is logical: each poll advances it by the poll interval, so deadlines and `STARVE_AFTER` still pass after the same number of polls. `try_update` goes through a small extension trait if shuttle's atomics only have `fetch_update`.
  - **Gates under test are fresh instances:** `Gate::new(&LOCAL)` with a shuttle thread-local, not the global `GATE`, and `WriteLock`/`Slots` constructed per test. The blocker is not installed (`run_blocking` calls the wait directly). `GENERATION` stays a plain std static (forks aren't modelled; the generation logic has unit tests).
  - **Models (each a few threads, a few thousand schedules in CI-sized runs, more in a `--slow` run):**
    1. Write lock: 3 threads each take and release `lock_until` twice, with a far deadline; an inside-counter asserts mutual exclusion; at the end `waiters` and `starving` are 0 and the lock is free. A variant with a hammering `try_lock` thread checks that the starving waiter gets in.
    2. `try_lock_nested` vs a starving waiter: never two holders.
    3. Slots: 4 threads `acquire_until` with cap 2; never over the cap; all finish.
    4. Gate: 2 threads in nested `enter_shared` levels and 1 forker in `begin_fork`/`end_parent`; when `begin_fork` returns, no other thread holds a level outside `parked`; no thread enters while the claim is pending (unless it already holds one); all finish.
    5. Gate: two forkers that each hold their own level (the D-020 two-forkers case): they take turns, never both claimed; all finish.
    6. Held shares dropped on another thread while the owner parks (the `parked` accounting's documented transient).
  - **Running:** `scripts/model-check.sh` (`RUSTFLAGS='--cfg shuttle' cargo test -p diskcache-oxide-core --features ... model_`), a target dir of its own; added to `scripts/check.sh` with a short budget, a longer one under `--slow`.
- **Step 7, as landed (2026-10-29, night).** The architect built the design in a scratch crate before reviewing it, and **the models found two real fork-gate bugs on their first run.** Both let `begin_fork` return while a third thread still held a running level, so the fork could copy a half-finished SQLite operation (the D-012 failure the gate exists to prevent):
  - **A, a stale `own`:** `begin_fork` read its own levels once, before its quiescence loop. A held share of the forker's dropped on another thread meanwhile made it count a third thread's level as its own. `quiescent()` now re-reads own levels every poll, after the counts.
  - **B, two loads that aren't a snapshot:** `quiescent()` read `holders`, then `parked`; a nested entry and a park in between looked quiescent (the two-forkers case, D-020). Both counts now share one `AtomicU64` (holders low, parked as a wrapping i32 high), read in one load; a share dropped while its owner is parked takes both off in one step.
  - Reproduced independently: on the unfixed gate the two-forkers and share-dropped-elsewhere models fail at 100k schedules; with only A fixed, both still fail. Neither soak saw them.
  - **Differences from the note:** shuttle is a `[target.'cfg(shuttle)'.dependencies]` normal dependency (the shim's non-test code uses it), not a dev-dependency, and `cargo deny` sees it (it passes). shuttle 0.9.9 has `try_update` and `const fn` atomics, so there's no extension trait, and `static GATE` compiles under the cfg. `sleep` under shuttle is the logical clock's advance plus `yield_now` (so PCT deprioritizes pollers), and a 60 s logical-time watchdog reports livelock (shuttle's deadlock detector never fires on sleep-poll loops). `STARVE_AFTER` is 200 µs under the cfg. Models 1b and 4 assert counts and an exclusivity witness rather than durations or "no entry while pending". Model 7, the composition (write lock, slot, gate, nested write, forker), was added.
  - **Review follow-ups applied:** one gate per OS thread, reset each execution, instead of one leaked per execution (1M schedules: 45 MB peak, not the projected 2.5-3 GB); `debug_assert` on every holders decrement; "reached" guards so no model passes vacuously (a fork that waited for a running level, the waiter starving, a share dropped while its owner was parked); `DCO_MODEL_REPLAY` replays a printed schedule. Reintroducing bug B fails a model at 100k.
  - **Running:** `scripts/model-check.sh [iterations]`, in `scripts/check.sh` at 100k (about 3 s) or 1M under `--slow` (about 25 s).
- **`clear()`'s pause and yield between chunks, measured for Michael's decision (2026-10-29, night).** `clear()` of 100k rows on the laptop, median of 3, 3.14 and 3.14t alike:

  | Variant | idle | 2 writer threads | their p99 / worst wait | writer in another process | its p99 / worst |
  |---|---|---|---|---|---|
  | A, today: 100 µs pause every chunk, then yield up to 2 ms while a writer waits | 0.29 s | 2.4 s | 0.3-0.6 / 20 ms | 2.33 s | 0.11 / 2.3 ms |
  | B: pause and yield only while an in-process writer waits | 0.13 s | 2.4 s | 0.2-0.5 / 19 ms | 0.29 s | 0.06 / 4.0 ms |
  | C: yield up to 500 µs | 0.29 s | 0.93 s | 1.1 / 15-20 ms | | |
  | D: no pause, no yield | 0.13 s | 0.13 s | 0.04 / **127-140 ms** | | |

  D shows the yield earns its keep: without it a writer waits out the whole `clear()`. B is 2.2× faster idle and 8× faster beside another process's writer, at the price of that writer's worst wait rising from about 2.3 to 4 ms (the unconditional pause was the gap other processes used). C halves the contended time for a higher writer p99. Recommendation: B, and keep the 2 ms yield. **Michael (2026-10-29): adopted.** `pause_between_chunks` now returns at once when no same-process writer waits; `MAX_YIELD` stays 2 ms. The clear() fairness test passed 20/20 on 3.14 and 3.14t after the change.
- **Linux throughput, investigated (2026-10-29, morning; Michael: an hour at most).** The first Linux soak runs on the mini reached about 20k of a 100k ops/s target, with `set` at 600 µs p50 and 11-17 ms p99. Two causes, neither in the cache:
  1. **The caches sat on the host folder shared into OrbStack's VM,** so SQLite paid the file-sharing layer on every lock, mmap and fsync. Moved to the container's disk (`DCO_SOAK_CACHE_ROOT`): `set` p50 600 → 200 µs.
  2. **OrbStack's VM kernel rounds every sleep up to about 3 ms** (a 40 µs `Event.wait` takes 2,986 µs; macOS 69 µs; with or without a CPU limit, timer slack 50 µs). The soak paced each call with a ~40 µs wait, which capped every thread at a few hundred calls a second. A single GIL worker alone, Michael's check, made only 4.6k ops/s, which ruled out cross-process locking. The worker now sleeps only when 5 ms ahead and catches up in bursts (same average rate): the single worker then made **99.5k ops/s, `get` p50 2 µs and p99 7-14 µs, `set` p50 26-38 µs and p99 1.1 ms**, on par with or faster than macOS.
  - **A lead for Phase 4:** the cache's own lock waits sleep-poll at 50 µs, so on a kernel with timers this coarse each poll costs 3 ms, which likely explains the 11-17 ms write p99 in the multi-process Linux runs. Real Linux servers (high-resolution timers) should sleep in about 60 µs; check on a Talk Python server before acting. The cheap fix already on the roadmap (spin a few µs, then `yield_now`, before each sleep) or real parking (a futex) would make waits independent of the timer.
- **Architect follow-ups on the `clear()` pause change:** the docs now say a writer in another process gets in between chunks usually, not by guarantee; `chunked`'s doc matches; and the next chunk takes the lock back with priority only after a real yield (`pause_between_chunks` returns whether it stepped aside).
- **Soak results (2026-10-29).**
  - **macOS, overnight (commit 57bd70e, before tonight's fixes):** PASS. 8.4 h at half the mini (7 processes × 4 threads, 3.14 and 3.14t, 180k ops/s): `get` p50 8 µs, p99 about 60 µs; `set` p50 73 µs, p99 2.5-6 ms; no drift between the first and last hour. No minute had a write over 500 ms (worst 378 ms; 5 minutes over 100 ms). 161 kills, 192/192 forks, 385 maintenance calls, 0 timeouts, stalls or orphans. Long-lived workers ended lower than they started (3.14 47.6 → 37.8 MiB, 3.14t 100.3 → 89.5 MiB), fds flat at 13 and 17. WAL peak 32 MiB.
  - **Linux (OrbStack, Ubuntu 26.04, 10 CPUs; commit e12ffb1, with the gate fix, restart chaos and fixed pacing):** PASS. 6 h, 5 processes × 4 threads, 100k ops/s: `get` p50 4-5 µs, p99 about 55 µs; `set` p50 about 67 µs, p99 about 15 ms (likely the 3 ms VM sleeps in lock waits, see above); no drift. Worst write 242 ms, one minute over 100 ms, none over 500 ms. 122 kills, **64 graceful restarts**, 126/126 forks, 221 maintenance calls, 0 timeouts, stalls or orphans. Long-lived workers flat (3.14 34.3 → 34.2 MiB, 3.14t 64.4 → 64.0 MiB); the 3.14t worker's fds stepped once from 18 to 20 at about 2.8 h and held (one more pooled connection, capped by the pool). WAL peak 32 MiB, ledger 15 MiB.
  - Still needed for the exit: a clean 24-hour macOS run on the final code (after Michael's reviews).
- **Step 8, review 07 (Angie's review, 2026-10-29, evening).** Five findings, all real; two were already fixed by the merged review work (the nested-operation deadlock by `60bcb1c`, the child's leftover fork claim by `d65a0ea`). The other three, each reviewed by the architect:
  - **Spilled values lost on rollback (critical, reproduced):** a write inside a held transaction unlinked the replaced row's value file before COMMIT, so a rolled-back set, add, delete, pop, incr, `clear()`, `expire()` or `cull()` lost the committed value, and a rolled-back set orphaned its new file. Writes that touch value files now go through `Store::write_files`, which records them on the held transaction while the operation still has its connection; `close_pinned` hands back the replaced files after COMMIT or the created ones after a rollback, unlinked once the write lock and gate are released (`7811698`).
  - **The store pin opened and closed outside the fork gate (medium, widened by `f93e766`):** since the pin became a SQLite connection, its open in `Store::open` and `add_handle`, and its close in three places (a GC'd store, a pin for another file, a failed open), took SQLite's process-wide mutex without the gate. `open_pin` now needs a gate guard, and `FilePin`'s `Drop` closes under the gate, or forgets a pin inherited across fork (`f3ab8e3`).
  - **`checkpoint()` and `close()`'s checkpoint kept the interpreter (low):** the copy itself already ran detached, but each busy round took the interpreter back after its write-lock wait, its checkpoint and its backoff (up to a 5 ms switch interval each under contention). They now run inside `crate::detach`, `close()` only when a checkpoint will run (so the usual close doesn't wait to get it back), and with the handle release inside, since the last connection's close may run SQLite's own checkpoint (architect review). Smaller than the review suggested: SQLite's own auto-checkpoint keeps the WAL near 4 MB, and a manual checkpoint took about 2 ms; it costs more when other processes' readers keep the WAL from resetting.
  - **Open follow-up:** `close(checkpoint=False)` and a `Cache` dropped without `close()` still release the last handle attached. With no other process on the file, the last connection's close runs SQLite's own checkpoint and deletes the WAL, with the interpreter held. Detaching when it's the last handle and the WAL is over its trim size would cover both.
  - **Open follow-up, for Michael:** inside a held transaction an operation has no SAVEPOINT, so one that fails partway (`set` stores its row, then its cull errors) leaves its row change in the transaction. If Python catches the error and the transaction commits, the row points at a value file that was removed (it reads as a miss, so nothing is served wrong) and the file it replaced is orphaned. A SAVEPOINT around each operation inside a hold would make it all-or-nothing; older than these fixes, and best as its own step before `transact()` (Phase 6b).
