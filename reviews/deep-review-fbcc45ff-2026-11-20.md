# Adversarial Review of `diskcache-oxide` `fbcc45ff` (Plan 32 Step 9 Fixes, 0.5.8 Gate)

- **Commit reviewed:** `fbcc45ff` (branch `plan-32-fork-lock-records`, PR #7; cloned at `6e0dc6d1` which adds the review handoff on top of `fbcc45ff`)
- **Diff reviewed:** `git diff 03ad2038..fbcc45ff -- . ':!site'` (against the `03ad2038` review), plus the full branch against `main`
- **Clone:** `/tmp/dco-review-fbcc45ff`
- **Date:** 2026-11-20
- **Reviewer:** Antigravity
- **Verdict:** **NO-GO for 0.5.8 as-is, with two small targeted fixes required.** Step 9 cleanly closed all six findings from the `03ad2038` review (H1, H2, M1, the accepted part of M2, M3, M4): `catch_up_in_child()` and `CHILD_RESET_PID` prevent early-hook bypasses without double-running or re-entering; `Store::open_shards` pre-checks every shard before opening or joining any; `add_handle_until`, `sweep_value_dirs`, `evict(None)`, and `remove_old_formats()` refuse before doing work; `Caches` and `DjangoCache` drop inherited failure windows on PID change; `next_store_seq!`, `sound_next_seq!`, and `check_totals` handle damaged `state.next_seq` and totals; and `(filename, id)` paging visits empty and duplicate filenames without sorting. Follow-up adversarial review found **0 Critical, 0 High, 1 Medium, and 1 Low** finding: (M1, reproduced) a nested `with cache.transact():` (`TxState::PassThrough`) inherited across `os.fork()` exits silently as `Done(committed)` in the child instead of raising `ForkedInsideTransactionError`; and (L1, traced) `check_usable_at` skips `poison::all_unusable()` when the database file does not yet exist, allowing a globally poisoned child to create a brand-new `Cache` or `FanoutCache` on disk and return a handle before failing on the first operation. All four known limits logged by the internal review are **accept as logged**, and the untested `Entering` transaction branch across `fork()` passed live testing.

---

## Summary of Findings

| ID | Severity | Tier | Where | Title |
|---|---|---|---|---|
| M1 | **Medium** | Patch | `crates/diskcache-oxide-py/src/cache.rs:2447-2449, 2483-2486` | `Transaction::__exit__` on a nested (`TxState::PassThrough`) transaction inherited across `os.fork()` marks it `Done(committed)` and returns `False` instead of raising `ForkedInsideTransactionError` |
| L1 | **Low** | Full | `crates/diskcache-oxide-core/src/store.rs:2598-2600`, `crates/diskcache-oxide-py/src/cache/fanout.rs:1455` | `check_usable_at` ignores `poison::all_unusable()` when the database file does not exist yet, letting a globally poisoned child create a new `Cache` or `FanoutCache` on disk and return a handle |

---

## Medium Findings

### M1. Nested (`TxState::PassThrough`) transaction exits silently as `Done(committed)` in a forked child instead of raising `ForkedInsideTransactionError`

- **Severity:** Medium
- **Where:** `crates/diskcache-oxide-py/src/cache.rs:2447-2449, 2483-2486` (`Transaction::__exit__`).
- **Trigger:** A thread enters an outer `with cache.transact():` (state `TxState::Held`, `entered_in = parent_gen + 1`) and an inner `with cache.transact():` on the same cache (state `TxState::PassThrough`, `entered_in = parent_gen + 1`), then calls `os.fork()`. In the child, leaving the inner `with` block calls `inner.__exit__(...)` (either normally or with an exception, and either after `_after_fork_in_child` or from an earlier `after_in_child` hook).
- **Effect:** `crossed_a_fork()` returns `true`, and `state_after_fork()` returns `Some(TxState::PassThrough)`. Line 2447 only refuses `TxState::Entering | TxState::Ending`, so execution falls through to the `TxState::PassThrough` arm at lines 2483–2486, which sets `*state = TxState::Done { committed: true }` and returns `Ok(false)`. The inner transaction silently succeeds and reports `<diskcache_oxide.Transaction Done(committed)>` in a child where the cache is unusable and every other transaction state (`Held`, `Entering`, `Ending`, and `Done` on an unusable store) raises `ForkedInsideTransactionError`. If the inner block is inside a helper that returns, catches, or exits via `os._exit(0)` before unwinding the outer block, no error is raised at all.
- **Evidence:** **Reproduced** on `fbcc45ff` (3.14): running the script below prints `CHILD inner.__exit__ SUCCEEDED: False repr: <diskcache_oxide.Transaction Done(committed)>`.
- **Confidence:** Confirmed.
- **Fix idea:** Include `TxState::PassThrough` in the `crossed` refusal at `crates/diskcache-oxide-py/src/cache.rs:2447`: `if crossed && matches!(*state, TxState::Entering | TxState::Ending | TxState::PassThrough) { return Err(to_py_err(Error::ForkPoisoned)); }`. That handles both normal child exits and early `after_in_child` hooks (since `crossed_a_fork()` runs `gate::catch_up_in_child()` first), leaves the transaction un-committed, and matches `Held`, `Entering`, and `Ending`.
- **Reproduction:**
  ```python
  import os, tempfile, warnings
  from diskcache_oxide import Cache

  with tempfile.TemporaryDirectory() as d:
      c = Cache(d)
      c.set('k', 1)
      with warnings.catch_warnings():
          warnings.simplefilter('ignore', RuntimeWarning)
          outer = c.transact()
          outer.__enter__()
          inner = c.transact()
          inner.__enter__()
          pid = os.fork()
          if pid == 0:
              res = inner.__exit__(None, None, None)
              print('CHILD inner.__exit__ SUCCEEDED:', res, 'repr:', repr(inner))
              os._exit(0)
          os.waitpid(pid, 0)
          inner.__exit__(None, None, None)
          outer.__exit__(None, None, None)
      c.close()
  ```

---

## Low Findings

### L1. `check_usable_at` skips `poison::all_unusable()` when the database file does not exist yet, letting a globally poisoned child create a brand-new cache on disk

- **Severity:** Low
- **Where:** `crates/diskcache-oxide-core/src/store.rs:2598-2600` (`check_usable_at`), called from `Store::open_until` (`store.rs:641`), `Store::open_shards` (`store/shards.rs:82`), and `layout::read_format` (`layout.rs:285`); also `crates/diskcache-oxide-py/src/cache/fanout.rs:1455` (`open` calls `fanout::open_manifest` before `Store::open_shards`).
- **Trigger:** A forked child inherits the pool registry (`POOLS`) locked (for example, an unhooked `fork()` racing with `Pool::new` or `Pool::drop`), so `end_fork_child` calls `poison::mark_all_unusable()`, meaning no database is usable in this process. The child then opens `Cache(new_dir)` or `FanoutCache(new_dir)` at a path that does not exist yet.
- **Effect:** `check_usable_at(&db_path)` calls `file_id_at(&db_path)`, which returns `None` for a nonexistent file and maps `None` to `Ok(())`. `Store::open_until` therefore proceeds to call `open::connect`, `open::prepare_creating`, and `open_pin`, creating `new_dir`, creating `diskcache-oxide.sqlite`, writing schema and limits to disk, and returning `Ok(store)`—only for the very first operation on that handle to fail with `Error::Unusable` in `store.lease()`. For `FanoutCache`, `fanout::open_manifest` also creates the root directory and manifest before `open_shards` runs.
- **Evidence:** **Traced line by line** across `store.rs:640-665`, `store.rs:2598-2600`, `store/shards.rs:79-84`, `poison.rs:76-90, 151-168`, and `fanout.rs:1455-1488`.
- **Confidence:** Confirmed.
- **Fix idea:** Check `if crate::poison::all_unusable() { return Err(Error::Unusable); }` at the top of `check_usable_at`, and in `crates/diskcache-oxide-py/src/cache/fanout.rs:open` check `gate::catch_up_in_child(); if poison::all_unusable() { return Err(Error::Unusable); }` before `fanout::open_manifest`.

---

## Verdicts on the Four Known Limits

1. **The numbering can hand out `2^62 - 1` after a row damaged to exactly `2^62 - 2`, and `check(fix=True)` then removes that last row:** **Accept as logged.** Any finite ceiling `T` has the same one-value boundary at `T - 2`: refusing `T - 1` in `next_store_seq!` moves the boundary down by one without removing it. Normal counting cannot reach `2^62 - 2` (146 years at 1 billion writes/sec), so triggering this requires corruption landing on a single 64-bit integer value (`2^62 - 2`).
2. **32,768 rows each damaged to a `size` near `2^48` would overflow the sum in `check()`:** **Accept as logged.** `MAX_SIZE = 2^48` rejects random 64-bit corruption outside `[0, 2^48]`, so overflowing `i64::MAX` in `SUM(sound_size!())` requires at least 32,768 distinct rows all simultaneously corrupted into the upper half of `[0, 2^48]`. Even then, SQLite raises an integer-overflow error cleanly with no panic and no silent corruption.
3. **Two threads of a child leaving one inherited transaction at the same moment can both be told it was lost to the fork:** **Accept as logged.** In a child that crossed a fork (`crossed_a_fork() == true`), `state_after_fork()` must use `try_lock()` because a vanished parent thread may have held `self.state`. For any transaction still open at the fork, `ForkedInsideTransactionError` is already the required outcome; the only case where the losing thread's result differs is two child threads racing on `__exit__` of an already-completed parent transaction on a still-usable cache, which is not a realistic pattern and cannot be avoided without risking deadlock on an inherited mutex.
4. **A `check(fix=True)` that repairs `next_seq` while a `clear()` is running can let that `clear()` remove writes made after it began:** **Accept as logged.** Triggering this requires `state.next_seq` to already be corrupted on disk, a multi-chunk `clear()` to be mid-flight, a concurrent `check(fix=True)` to finish `check_totals` between chunks, and a new write to land before `clear()` finishes its remaining chunks. Preventing it would either block `clear()` on a damaged `next_seq` (re-introducing part of M3) or require extra on-disk state.

---

## Checked and Found Sound

- **`gate::catch_up_in_child()` and `CHILD_RESET_PID` (`gate.rs`):** Verified line by line. `CHILD_RESET_PID.swap(pid, AcqRel) == pid` ensures the child-side reset runs at most once per child PID; `std::process::id() == FORKING_PID` prevents it from ever running in the parent; `GLOBAL_THREAD.try_with(|t| t.forking.get())` ensures only the forking thread triggers it; `CHILD_RESET_PID.swap` executes before any other work in `end_fork_child`, preventing re-entry from inside `end_fork_child`; and a subsequent `begin_fork()` in the child updates `FORKING_PID` to the child's PID so a grandchild gets its own reset cleanly.
- **`Transaction.entered_in` across two forks and untested `Entering` branch (`cache.rs`):** Reproduced both a grandchild leaving a grandparent-entered transaction and a grandchild leaving a child-entered transaction (after a clean first fork)—both raised `ForkedInsideTransactionError`. Also tested the previously untested `Entering` branch (Thread B blocked in `tx.__enter__()` while Thread A holds an outer transaction and forks): in the child, `repr(tx)` returned `<diskcache_oxide.Transaction Entering>`, `tx.__enter__()` raised `RuntimeError`, and `tx.__exit__` (both normal and exceptional) raised `ForkedInsideTransactionError` in 0.1 ms with no hang.
- **`Store::open_shards` pre-check window (`store/shards.rs`):** Verified that the unusable set in a given process only grows during `end_fork_child` (never while the process is running normally after the child reset), so checking every shard under one gate level before opening or joining any shard has no TOCTOU window in the same process. Existing directories and manifests are read-only in `open_manifest` (for nonexistent directories under `all_unusable()`, see L1).
- **`caches.py` (`failures_pid`) and `djangocache.py` (`unavailable_pid`, `forget_the_parents_failures`):** Verified on `fork`, `spawn`, and `forkserver` semantics. Both clear inherited failure maps on PID change and re-raise `ForkedInsideTransactionError` without recording a cooldown. On free-threaded Python (3.14t), a race between two child threads on the lock-free PID check can at worst clear a newly recorded operational failure from the first thread (causing one extra retry) and can never serve an inherited parent `NullCache` or suppress `ForkedInsideTransactionError`.
- **`check_totals` with every `state` column and every row's `store_seq` damaged at once (`check.rs`, `rows.rs`):** Tested on `fbcc45ff` with `state.count = 'bad'`, `state.size = 'bad'`, `state.next_seq = 'bad'` and all rows having `store_seq = 'bad'`. `check(fix=False)` reported all five findings; `check(fix=True)` repaired the table and `state` completely (`check(fix=False)` clean afterwards) and the next write received `store_seq = 1`. (Minor cosmetic observation only: when `check_column_types(fix=True)` deletes damaged rows before `check_totals`, `adjust_totals` turns a text `state.count`/`state.size` into an integer via SQLite subtraction, so `check(fix=True)` reports `Count(-2, 0)` instead of `BadState(count)` and omits `BadState(size)`, while still repairing both columns accurately.)
- **`CHECK_ROWS_SQL` `(filename, id)` paging across storage classes (`check.rs`):** Verified against `TEXT` (valid, missing, empty `''`), `BLOB`, and `NULL` filenames. SQLite orders `INTEGER/REAL < TEXT < BLOB`; starting at `(TextBytes(b""), i64::MIN)` skips integer/real filenames, visits all `TEXT` filenames including `''` and duplicate names across page boundaries on `USING INDEX cache_filename` with no `TEMP B-TREE`, and filters trailing `BLOB` filenames via `typeof(filename) = 'text'`.
- **Mutant check on the new Rust tests for H2, M1, M3, M4 (`store/shards.rs`, `store/fork_timeout.rs`, `rows.rs`, `check.rs`):** Inspected all new Rust tests against the `03ad2038` code (`a_reopen_with_one_shard_unusable_changes_no_other_shard`, `a_closed_handle_on_an_unusable_store_stays_closed_and_opens_no_pin`, `a_store_seq_or_floor_at_the_ceiling_fails_the_write_rather_than_repeat`, `a_damaged_next_seq_fails_writes_is_reported_and_fix_repairs_it`, `a_row_at_the_last_store_seq_never_leaves_the_cache_unwritable_after_fix`, `a_damaged_total_is_reported_and_recounted_never_raised`, `check_reports_an_empty_filename_and_fix_removes_its_row`, `rows_sharing_a_filename_are_all_visited_across_a_page_boundary`). Every test asserts the exact behavior that failed on `03ad2038` and passes on `fbcc45ff` (`cargo test -p diskcache-oxide-core --lib`: 505 passed, 0 failed).
