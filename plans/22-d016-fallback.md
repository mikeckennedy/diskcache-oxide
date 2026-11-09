# Plan 22: Test the D-016 locked fallback, and fix its inline miss

Written 2026-11-08, from the coverage run (`scripts/coverage.sh`). `Store::get_with`'s locked fallback ([ops.rs](../crates/diskcache-oxide-core/src/ops.rs), after the optimistic loop) is the part of D-016 that guarantees a reader can't keep losing to a fast writer, and no test has ever run it: it needs `spill::OPTIMISTIC_ATTEMPTS` (8) optimistic reads in a row to find their value file replaced. `Store::pop`'s spilled-value and missing-key branches are also unrun.

## The bug

The fallback looks the key up with `file_row`, which maps `Found::Inline` to `None`, and then returns `Ok(None)`. So when the write that beat the eighth optimistic read replaced the spilled value with a small one (stored inline), `get` reports a miss for a key that was present the whole time. D-016 promises that can't happen.

Confirmed with a probe test (step 1's hook): eight overwrites, the last to `"small"`, and `get` returns `None`.

Impact: it needs eight consecutive lost races ending in a large-to-small overwrite, and the cost is one spurious miss (a recompute), never a wrong value or lost data. The same class of bug D-016 fixed, much rarer.

## Release

Plan 21's rule is that the soaked code doesn't change, and 0.1.0's 13 artifacts are built from `2a323a5`. Decision (Michael, 2026-11-08): the fix goes into 0.1.1. 0.1.0 ships as built; the fix lands on `main` under Unreleased.

## The cause, more precisely (architect review)

Two defects. The loop's last re-read finds the right answer (an inline hit, already decoded, or a miss or a refused file) and throws it away when the loop ends; then the fallback's `file_row` can't see inline rows. The first is the source of the wrong miss and also wastes a decode (which may have run Python); the second is still reachable when a write lands between the last re-read and the write lock.

## Step 1: Deterministic hooks

Two `#[cfg(test)]` module-level thread-locals in `ops.rs`, as `store.rs`'s `FAIL_NEXT_END_BUSY`: `BEFORE_OPTIMISTIC_READ`, called in the loop just before `spill::read`, and `BEFORE_FALLBACK`, called just before the fallback's write. Each holds a `Box<dyn FnMut(&Store)>`, taken out while it runs (so a hook may call `get` again) and cleared by a drop guard. An overwrite from a hook makes the read lose: `write_files` unlinks the replaced file synchronously after the commit, so `spill::read` finds it missing and the loop re-reads a new version.

## Step 2: The fix

1. **After the loop**, the last re-read is examined: a miss, an inline hit or a refused file returns at once; only an untried file goes to the fallback.
2. **The fallback** looks up the live row of any kind under the write lock with `look_up` and a decode that copies the value out (`owned(ValueRef) -> SqlValue`, shared with `pop`), returning a named `Locked` enum: `Miss`, `Inline`, `Refused`, `Opened`. An inline value is decoded after the lock is released (decode can run Python that uses the cache). `file_row` goes. A comment notes that in a doomed held transaction the fallback's write gives `TransactionAborted` (practically unreachable: inside one's own hold nothing else can replace the file).
3. **D-013 on both paths:** a file missing or short under the lock is a true miss, and its version is deleted with `delete_version_nowait` after the lock is released, as the loop does.

## Step 3: Tests

On the 8th `BEFORE_OPTIMISTIC_READ` call (the post-loop check) and again on `BEFORE_FALLBACK` (the fallback's arms):

1. Another spilled overwrite: the latest value (the fallback's open-and-read path, from either hook).
2. An inline overwrite: that value, not a miss; and `decode` runs once.
3. A delete: a miss.
4. An overwrite with a refused mode whose file the hook then unlinks: `decode` sees `ValueRef::Null`, which proves the file was never opened.
5. The 8th call overwrites, then another connection (`crate::open::connect`) takes `BEGIN IMMEDIATE`: the fallback waits out the budget (about 400 ms) and raises `Timeout` whose `seconds` is the call's whole timeout. Rolled back before the guard drops. Never `begin_hold` on another thread (it would hold a gate level against another test's fork).
6. Under the lock the file is gone or short (`BEFORE_FALLBACK` unlinks or truncates it): a miss, and the row is removed at once by the fallback's own cleanup (D-013), checked with forks held off so that cleanup can't skip.

The D-020 branch (a forked child that can't write reports a miss) needs a fork and is out of scope: it is the only fallback code expected to stay unrun.

## Step 4: `Store::pop` gaps

Direct unit tests: `pop` of a spilled value returns its bytes and removes the file; `pop` of integer and real values (`Value` has no null); `pop` of a missing key is `Pop::Missing`; `pop` of a spilled row whose file is gone is `Pop::Missing` and the row goes.

## Step 5: Docs and records

- `get_with`'s doc: an inline value the fallback finds is decoded with no connection held. `spill.rs`'s module doc: the fallback takes the row under the write lock and opens its file or copies an inline value.
- `tests/DIVERGENCES.md` D-016: name the new core tests.
- `CHANGELOG.md` Unreleased, Fixed: "a read of a large value that kept losing to overwrites could report a miss when the last overwrite was small".
- No user guide change: the guide already states the D-016 behavior the fix restores.

## Done when

`cargo clippy --all-targets -- -D warnings`, `cargo test`, `cargo fmt --check`, `RUSTDOCFLAGS="-D warnings" cargo doc --workspace --no-deps`, the unit and oxide conformance suites on 3.14 and 3.14t, and `scripts/coverage.sh` showing the fallback covered. The architect reviews this plan and the finished change.

## Log

- 2026-11-08: plan written; the bug confirmed with a probe test. Michael: the fix goes into 0.1.1. The architect reviewed the plan and agreed with the diagnosis and the release call; its required changes (examine the last re-read after the loop, the `Locked` enum and shared `owned`, a second hook before the fallback, test 5's mechanics, the doc updates and `cargo doc`, the spilled-row-gone `pop` case, dropping the probe) and suggestions (re-entrant module-level hooks, proving the refused file is never opened, D-013 cleanup in the fallback, counting decodes) are folded in.
- 2026-11-08: implemented. The loop examines its last re-read; the fallback handles every row kind through `Locked`, decodes inline values after the lock, and removes a version whose file is missing or short under the lock. Eight new core tests (the fallback's six branches, each from both hooks where it applies, and two `pop` gaps). The architect reviewed the finished change and found it correct; its required changes (assert the fallback's own D-013 cleanup with forks held off, test the short-read arm) and suggestions (a lower bound on the timeout wait, a hook that refuses to be overwritten, the doc rewrap, this plan's text) are in. Gate: clippy, fmt, `cargo doc` clean; the unit and oxide conformance suites green on 3.14 (1,658 passed); on 3.14t one WAL timing test (`test_auto_mode_shrinks_the_wal_back_after_a_burst_with_readers_in_other_processes`, a busy-ratio bound) failed 2 of 4 runs alone on a loaded Mac. `cargo test` fails two `store::fork_timeout` timing tests on this loaded Mac, the same on a clean export of HEAD, so not this change; they need a rerun on a quiet machine. `scripts/coverage.sh`: `ops.rs` lines 92.1% to 95.8%; in `get_with` only the D-020 miss and `whole_timeout`'s non-timeout arm stay unrun. `coverage.sh` now carries on past a failing `cargo test` and reports it.
- 2026-11-08: a pattern hunt over every production retry loop in core and the py crate (an independent agent, given the bug shape and the code) found no second instance of the discarded-last-result shape. It found one small bug: a closed `Cache` handle used again subtracted the time spent rejoining its store before the call, so a `Timeout` named the leftover (`0.199793708 s`) instead of the budget, on `set`, `add`, `delete`, `touch`, `incr`, `expire`, `clear` and `cull` (`pop` already remapped it). Fixed with `StoreHandle::run`, which remaps only when the handle had to rejoin; `test_a_reused_closed_handles_timeout_names_the_whole_budget` fails 8 of 9 cases on the old code. For 0.1.1 with the rest.
