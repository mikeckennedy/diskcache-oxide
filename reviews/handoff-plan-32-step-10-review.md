# Handoff: third pass on plan 32, the step 10 changes (0.5.8)

Your second review (`reviews/deep-review-fbcc45ff-2026-11-20.md`) found one Medium and one Low, and you fixed both. Those fixes were pulled into the branch, reviewed internally, and changed in three ways. This handoff asks you to review what was done to your fixes, look once more at the branch as it now stands, and fix what you find. Same deal as last time: review first, then fixes. If you think one of the changes to your work is wrong, say so and show the failure.

## What to review

- Repository: `diskcache-oxide`, branch `plan-32-fork-lock-records` (draft PR #7), revision `3ca85e87`.
- The diff since your last review: `git diff fbcc45ff..3ca85e87 -- crates tests`. Your three code commits (`5cb7d20b`, `b467be28`, `3e0625e9`) are in it unchanged; `3ca85e87` is the internal follow-up on top. To see only that: `git diff e1765fa5..3ca85e87 -- crates tests`.
- Read first: the last entry of the Log in `plans/32-fork-lock-records.md` (step 10). The two earlier handoffs in `reviews/` still describe the branch and its claims.

## Where to work

As before: not the main working tree, which is running the release gate and then a 24-hour soak with the project installed editable. Your clone from last time works; bring it up to date with `git pull --ff-only` (your commits are ancestors of `3ca85e87`). Review, build, test and commit there. Don't push; report the hashes.

## What changed in your fixes

**M1, the nested transaction.** Your fix raised the fork error on `__exit__` for every `TxState::PassThrough` in a process forked since entry. That also refused a nested block whose outer transaction had ended before the fork (blocks left out of order with manual `__enter__`/`__exit__`, an `ExitStack`, or an end on another thread): no connection was out at the fork, the child can use the cache, `set` works there, and yet the exit said the cache was unusable.

- `crates/diskcache-oxide-py/src/cache.rs`, `Transaction::__exit__`: the unconditional refusal is back to `Entering | Ending`. The `PassThrough` arm now does what the `Done` arm does in a crossed process: `self.cache.handle().store.check_usable()` first, then the ordinary exit. A nested block whose outer was still open at the fork still raises, because that outer's connection poisoned the store.
- `tests/unit/fork_inherited_scenarios.py`: new scenario `nested-transaction-whose-outer-ended-before-the-fork`. Your `fork-inside-a-nested-transaction` is unchanged and still fails if the `check_usable` call is removed.

**L1, a new cache in a child that can use none.** Two gaps were found in the fix. A plain `Cache(new_dir)` still created `new_dir` and its parents before it was refused, because `open::ensure_directory` runs in `Store::open_until` ahead of `check_usable_at`. And the `FanoutCache` guard was in the PyO3 layer, so any other caller of the public `fanout::open_manifest` skipped it.

- `crates/diskcache-oxide-core/src/poison.rs`: `refuse_if_nothing_is_usable()` runs `gate::catch_up_in_child()` and fails with `Error::Unusable` when `all_unusable()` is set.
- It is called first in `fanout::open_manifest` (`fanout.rs`) and first in the plain-cache branch of `Store::open_until` (`store.rs`), in both cases before anything is created. The lines you added to `crates/diskcache-oxide-py/src/cache/fanout.rs` are removed. Your `check_usable_opt` stays as the check made under the gate.
- Tests: a test-only thread-local, `poison::PRETEND_NOTHING_IS_USABLE`, makes the helper refuse on one thread (the real flag is process-wide and never cleared). `fanout::tests::a_process_that_can_use_no_cache_creates_no_root` and `store::tests::a_process_that_can_use_no_cache_creates_no_directory_for_a_new_one` assert that a refused `open_manifest`, `Store::open` and `Store::open_resolving` leave no directory. Both fail with the helper call removed.

## Where I'd look hardest

- Every way a `PassThrough` can exist at a fork, against the claim "its store is unusable in the child exactly when the transaction it joined was still open at the fork": same-thread nesting, `transact(key=)` on a `FanoutCache` shard, a nested block owned by a thread other than the one that forked, an outer that another thread was ending (`Ending`) or had rolled back at the fork, and a grandchild.
- `self.cache.handle().store` for a transaction on a `FanoutCache` shard: is it the shard the block is on, or could it check the wrong store?
- The shard branch of `Store::open_until` has no call to the new helper: it relies on `open_shards`' pre-check and on `open_manifest` having refused first. Is there a path to a shard open that passes neither (a rebuild, `remove_old_formats`, the layout redirect in `open_resolving`, a `Caches` or Django open)?
- Anything else that writes to disk on an open before a usability check: the format subdirectory in `layout`, value folders, the manifest's `fsync` of the root's parent.
- The test-only thread-local: can it change production behaviour (it is behind `#[cfg(test)]` in the core crate only), and do the two tests prove the wiring or only the helper?
- The whole branch once more with fresh eyes, especially anything your first two passes marked "found sound" by reading rather than by running.

## Settled: don't re-report unless you can show it is worse than stated

- The four known limits from the last handoff. You judged each "accept as logged" and the owner agrees.
- A cache disabled in a `Caches` registry resolves to a `NullCache` in every process (owner's decision, 2026-11-20).
- New limit, logged in step 10: a thread started from another library's earlier `after_in_child` hook is not the forking thread, so `refuse_if_nothing_is_usable()` reads the parent's flag and that thread can create an empty directory before the check under the gate refuses its open.
- The state mutex inherited locked across a fork has no deterministic test. You said why; that stands.

## What has been run on `3ca85e87` (macOS arm64)

- `cargo fmt`, `cargo clippy --all-targets -- -D warnings`, `cargo test` (507 core lib tests), `cargo doc` with `-D warnings`, ruff, ty: pass.
- Unit suite: 2,024 passed on 3.14, 2,022 on 3.14t. Conformance against oxide: 506 and 508.
- Mutant checks: the two new Rust tests fail with the helper calls removed; `fork-inside-a-nested-transaction` fails with the `PassThrough` usability check removed.

- `bench.gate` against diskcache 5.6.3: GO on 3.14 and 3.14t.
- `scripts/check.sh` on 3.14, 3.14t, 3.15 and 3.15t: pass.

Running now on the same commit, results not in yet: sanitizer lanes A and B on macOS and C to E in the Linux container, then the 24-hour soak. `gate.rs` didn't change in step 10, so the shuttle models weren't rerun. On `fbcc45ff`, before your fixes, `check.sh` and lanes A, B and C passed. Nothing has run on Windows or on x86-64.

## How to build and run

Unchanged from the last handoff (`reviews/handoff-plan-32-step-9-review.md`, "How to build and run"). Never `uv sync`; `ulimit -n 10240` for the fd-heavy tests; heavy jobs under `nice -n 20`, six cores left free, and no benchmarks while the gate or the soak is running.

## What to deliver

**Part 1, the review.** `reviews/deep-review-3ca85e87-<date you finish>.md`, in the format of your last two: verdict with counts by severity, each finding with severity, where, trigger, effect, evidence (reproduced or traced, say which), confidence and a fix idea, then what you checked and found sound. Commit it on its own before changing code.

**Part 2, the fixes.** The rules of the last handoff's Part 2 apply unchanged: a failing test first, one commit per finding, a one-paragraph single-line commit message with no attribution lines, the conventions in `CLAUDE.md`, docs and `site/` updated when behaviour changes, green on 3.14 and 3.14t, and a dated Log entry in the plan. Stop and report instead of deciding when a fix would change documented public behaviour, need a new divergence, or change what is written to disk. The version bump, wheels, tags, PyPI, pushing and the pull request's state are out of scope.

**Hand back** the review file, your commits with one line each, test and lint results on your final commit, anything you stopped on, and what you could not verify. Say plainly whether your commits touch shipped code: if they do, the sanitizers and the 24-hour soak start over, so a finding that needs no code change is worth separating from one that does.
