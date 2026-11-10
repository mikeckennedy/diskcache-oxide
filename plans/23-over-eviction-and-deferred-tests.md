# Plan 23: The over-eviction fix and the tests held for after 0.1.1

Written 2026-11-09. Everything here changes `crates/`, which plan 21 freezes until 0.1.1 is published, so it all lands right after the release and ships as 0.1.2 (Michael's call, 2026-11-09). Nothing in this plan is in the 0.1.1 wheels.

## How these were found

A coverage run and a hand-picked mutation campaign on `e108542`: 55 plausible bugs planted one at a time in a scratch copy, 42 caught by the tests (76%), 13 survived. The survivors that were real gaps and testable from `tests/` are closed in `2b57026` (`tests/unit/test_totals.py`, a refused `add` leaving no file, the `Caches` busy-open retry, `close_all()` forgetting a failed open). The campaign's main finding was that every size-pressure test used inline values on oxide: `tests/conformance/test_eviction.py` says its values spill, and at 60,000 bytes they do on diskcache (threshold 32,768) but not on oxide (65,536). Running that suite at 70,000 bytes as well found the bug below.

## The bug: a write that sweeps expired large values evicts live entries it should keep

`cull` in [ops.rs](../crates/diskcache-oxide-core/src/ops.rs) removes expired rows first (`delete_expired`), then evicts the least recently stored only if the cache is at or over `size_limit`. It judges that with `before.size + size_delta`: the `totals.size` the prelude read before the write, plus the write's own change. The value files the sweep just removed are still in that number, so the cache looks over its limit although the sweep freed enough, and `delete_oldest` takes live entries with what's left of the `cull_limit` budget. Inline values don't show it, since their freed pages come off through `freelist_count`.

- Seen as: `test_expired_entries_are_culled_before_older_live_ones` at 70,000 bytes keeps `k010` onward and `big`, but loses the live `k000` to `k004`. diskcache 5.6.3 and the community fork pass.
- Impact: up to `cull_limit − swept` live entries evicted per such `set` (9 at the default), recomputed on their next miss. No wrong value, no leak; `totals` stays exact (`test_totals.py`). Only a size-limited cache that is at its limit, holding expired values over 64 KiB. Talk Python's `show_pages` (10 GiB of large values) is the cache that can meet it.
- Not a divergence: diskcache is right and oxide's own doc comment on `cull` says what it should do.

## Step 1: The fix

`RowWriter::delete_batch` already sums the removed rows' sizes for `adjust_totals`. Return that sum with the rows (from `delete_expired`, and `delete_oldest` for symmetry), and have `cull` judge the limit with `before.size + size_delta − freed`. No extra query. Confirmed in a scratch copy with a two-query form of the same subtraction: the failing case passes, with all 308 core tests, `test_totals.py` and `test_model.py`.

A core unit test beside `culling_shares_one_budget_and_evicts_least_recently_stored`: expired spilled values whose sweep brings the cache under its limit leave every live row in place; and one where the sweep isn't enough, so eviction still runs with the rest of the budget.

## Step 2: The held conformance change

`plans/23-test_eviction.patch` (apply with `git apply`, then delete the patch): `test_eviction.py` runs every test at 60,000 and 70,000 bytes through an autouse fixture, and adds `test_volume_comes_back_when_values_are_removed` (clear, expire, the sweep on set, delete, pop, overwrite with a small value). It passes on diskcache and the community fork today; on oxide its one failure is the bug above. It is not committed on its own because the suite would be red on main.

## Step 3: Tests of tricky code no test runs (from the coverage run)

Production code is at 96.2% of lines (260 of 6,827 unrun, both builds merged, forked children counted). The unrun parts worth a test, in order:

1. **A write whose `COMMIT` comes back busy** (`store.rs`, the `ROLLBACK` before the retry in `WriteTx`): every `set`, `add` and `incr` goes through it. A `cfg(test)` hook as `FAIL_NEXT_END_BUSY` is for the held end.
2. **A forked child that inherited its parent's held transaction** (`store.rs`'s `inherited` branches and `ForkPoisoned` at the end, `gate.rs`'s `ShareEntry::Inherited` and `enter_unbounded`). Web workers fork. Also the mutation survivor here: the `d020` fork scenario should write enough inside the hold to reach the WAL before forking, then assert every value reads back after the parent commits (closing the inherited pinned connection in the child survived every test, harm unverified).
3. **One deadline across the write-lock wait and the SQLite wait** (mutation survivor): a write that waits for a thread's write lock for 60% of its timeout while another connection holds `BEGIN IMMEDIATE` must time out within about 1.3 timeouts, not 1.6.
4. **The unclaimed-fork reset** (`gate.rs`, `reset_in_unclaimed_child`): a Rust test that forks without `begin_fork`. Deferred from plan 21 on 2026-11-08.
5. **Forced-checkpoint outcomes** (`store.rs`: busy and no-slot), and **the database file replaced while it is being opened**.
6. Small ones from the campaign: the write-time sweep's `expire_time < now` boundary (one row expired earlier, one expiring exactly now, then a `set`); `Decision::Nothing` with the WAL exactly at its limit; `delete_version_nowait` from inside a `get_with` decode returning `Skipped` (gate depth, not a busy lock, being what refuses); the D-016 fallback in a poisoned child reporting a miss (D-020).

Not worth a test: `const fn` constructors, `unreachable!` and `debug_assert!(false)` arms, the SQLite-initialized-first mutex statuses, the codec's serde fallbacks (plan 22's notes), `volume == size_limit` exactly, and the stale-idle-connection loop in the pool (equivalent: `close_idle_all` empties it before every fork).

## Step 4: Records

CHANGELOG Unreleased, Fixed: the over-eviction. `test_eviction.py`'s docstring already says both sizes once the patch is applied.

## Done when

`cargo clippy --all-targets -- -D warnings`, `cargo test`, `cargo fmt --check`, `cargo doc`, the unit and oxide conformance suites on 3.14 and 3.14t, the conformance suite against diskcache and the community fork, and `scripts/check.sh --coverage`. The architect reviews this plan before step 1 and the finished change. 0.1.2 then takes the release gates (sanitizers, a fresh soak, PGO wheels).

## Log

- 2026-11-09: plan written; the bug confirmed by the held test and by the fix in a scratch copy. Michael: 0.1.2, after 0.1.1 ships; the eviction test waits with the fix.
