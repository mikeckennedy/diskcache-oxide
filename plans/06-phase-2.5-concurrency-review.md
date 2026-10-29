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
