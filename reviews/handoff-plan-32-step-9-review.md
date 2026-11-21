# Handoff: review the step 9 fixes of plan 32, then fix what you find (0.5.8)

Your review of `03ad2038` (`reviews/deep-review-03ad2038-2026-11-20.md`) ended no-go with two High and four Medium findings. All six were checked against the code, found real, and fixed in one commit, `fbcc45ff`, except one part of M2 that the owner has since decided to keep. This handoff asks for two things, in order: review those fixes as hard as you reviewed the branch, then fix what you find. A fix that is wrong, incomplete or opened a new hole is the most useful thing you can report.

## What to review

- Repository: `diskcache-oxide`, branch `plan-32-fork-lock-records` (draft PR #7), revision `fbcc45ff`.
- The step 9 diff: `git diff 03ad2038..fbcc45ff -- . ':!site'`. `site/` is the generated docs site; ignore it. The whole branch against its base is `git diff main...fbcc45ff -- . ':!site'` (about 4,500 lines added across 57 files).
- Read first: `CLAUDE.md`, then the last two entries of the Log in `plans/32-fork-lock-records.md` (step 9 and the architect's review of it), then your own review, so each finding can be read beside its fix. The earlier handoff, `reviews/handoff-plan-32-follow-up-review.md`, still describes the branch as a whole; its ten "Claims to attack" still stand and are still worth attacking.

## Where to work

Don't use the main working tree. It is running the sanitizer lanes now and will run a 24-hour soak after them, and both environments install the project editable, so an edit there changes the code under test. Make your own copy:

```bash
git clone --branch plan-32-fork-lock-records <path to the repository> <a new directory>
```

Review, build, test and commit in that clone. Don't push; report the commit hashes and the owner will pull them.

## What step 9 changed, by finding

**H1, calls that read fork state without entering the gate.**

- `crates/diskcache-oxide-core/src/gate.rs`: `catch_up_in_child()` runs the child-side reset (`enter_child_early`) when the calling thread is the one that forked and our `after_in_child` hook hasn't run yet. `Store::check_usable` (`store.rs`) calls it first, and so does the transaction.
- `crates/diskcache-oxide-py/src/cache.rs`, `Transaction`: `entered_in` (the fork generation plus one, set at `__enter__` and kept for good) replaces `ending_in`. In a process forked since entry (`crossed_a_fork`), the state mutex is only tried (`state_after_fork`), a locked mutex or an `Entering` or `Ending` state raises the fork error, and a `Done` state asks `store.check_usable()` before anything else. That last branch is the "rolled back on another thread just before the fork" case, which used to raise `TransactionAborted`.

**H2, a `FanoutCache` opened with limits in such a child wrote them to the shards ahead of the unusable one.** `store/shards.rs`, `open_shards`: every shard's database path is checked under one gate level before any shard is opened or joined.

**M1, work done before the refusal.** `store.rs`: `add_handle_until` checks the poison registry right after entering the gate and before `claim()`, so a closed handle isn't reopened with a new pin; `sweep_value_dirs` returns early on an unusable store. `cache.rs` and `cache/fanout.rs`: `evict(None)` and `remove_old_formats()` check usability first, on every shard for a fanout.

**M2, a parent's failed opens served in the child.** `python/diskcache_oxide/caches.py` (`failures_pid`) and `djangocache.py` (`unavailable_pid`, `forget_the_parents_failures()`): a process whose pid differs from the one that recorded the failures drops them, so the child tries the open itself and gets the real error.

**M3, a damaged `state.next_seq`.** `rows.rs`: `next_store_seq!` yields a number only when the floor is an integer (`CASE WHEN typeof(floor) = 'integer' THEN NULLIF(MIN(MAX(max + 1, floor), 2^62), 2^62) END`), so any other floor fails the write on `NOT NULL`; `sound_next_seq!` lets `seq_snapshot` (`maintenance.rs`) ignore a damaged floor, so `clear()` runs. `check.rs`: a new finding, `BadState { column, found, fixed }`; `check_totals` reads `state` by type and, with `fix`, recounts the totals and sets `next_seq` to the largest sound `store_seq` plus one.

**M4, `check()` raising or missing rows.** `rows.rs`: a `size` outside 0 to 2^48 is damage (`MAX_SIZE`, `WRONG_TYPE`, `sound_size!`, `lenient_size`), and a `store_seq` of 2^62 - 1 or more is damage. `check.rs`: the pass by file name pages by `(filename, id)` from `(b"", i64::MIN)`, so an empty file name and rows sharing a name across a page boundary are all visited, still on the partial `cache_filename` index with no sort.

Tests: `tests/unit/fork_inherited_scenarios.py` and `test_fork_inherited.py`, `tests/unit/fork_scenarios.py` (scenario `d020`), `tests/unit/test_django.py`, `tests/unit/test_damaged_rows.py`, and new Rust tests in `check.rs`, `rows.rs`, `store/shards.rs` and `store/fork_timeout.rs`. Docs: `CHANGELOG.md` (Unreleased), `tests/DIVERGENCES.md` (D-013 and the `check` entry), `docs-source/user_guide/errors.qmd`, the `check` docstrings in `python/diskcache_oxide/_core.pyi`.

## Decided by the owner on 2026-11-20: don't re-report

A cache that is disabled in a `Caches` registry resolves to a `NullCache` in every process, including a child that couldn't use the real cache. Disabled means no caching by request, nothing is opened, and so there is nothing for the fork to break. This is the declined part of M2 and it stays.

## Four known limits: judge each one

The internal review logged these instead of fixing them. For each, say "accept as logged" or "needs a fix before 0.5.8", and for the second give a reproduction of a real failure, not the limit restated.

1. The numbering can hand out 2^62 - 1 after a row damaged to exactly 2^62 - 2, and `check(fix=True)` then removes that last row. Refusing the number was judged to move the same edge one down, to a cache nothing could repair.
2. 32,768 rows each damaged to a `size` near 2^48 would overflow the sum in `check()`.
3. Two threads of a child leaving one inherited transaction at the same moment can both be told it was lost to the fork.
4. A `check(fix=True)` that repairs `next_seq` while a `clear()` is running can let that `clear()` remove writes made after it began.

Two branches of the new transaction code have no test: a transaction another thread was still entering at the fork (`Entering`), and a state mutex inherited locked. A test for either that fails on a real fault counts as a finding; a test that passes is a welcome addition.

## Where I'd look hardest

- `catch_up_in_child` and `CHILD_RESET_PID`: can the reset run twice, run in the parent, run on a thread that didn't fork, or run re-entrantly from inside `end_fork_child`? What does a child of the child see?
- `entered_in` across two forks (a grandchild leaving a transaction its grandparent entered), and across a fork in a process that entered the transaction after an earlier fork.
- The shard pre-check in `open_shards`: the window between the check and the opens, and whether the check itself can create a file or a directory in a child that should touch nothing.
- The pid check in `caches.py` and `djangocache.py` on free-threaded Python, with two threads of the child resolving at once, and under `multiprocessing`'s `spawn` and `forkserver` (where nothing is inherited and nothing should be dropped wrongly).
- `check_totals` with every `state` column damaged at once, and the repair of `next_seq` when every row's `store_seq` is damaged too (what number does the next write get?).
- The `(filename, id)` paging against rows whose `filename` is text, a BLOB, an integer or NULL, since SQLite orders those by storage class first.
- The mutant check was run only on the Python side: the five new fork and Django tests fail on the unfixed code. The new Rust tests for H2, M1, M3 and M4 were not run against unfixed code, so one of them could pass for the wrong reason.

## What has been run on `fbcc45ff` (macOS arm64)

- `cargo fmt`, `cargo clippy --all-targets -- -D warnings`, `cargo test` (505 core lib tests), `cargo doc` with `-D warnings`, ruff, ty: pass.
- 28 shuttle models of the fork gate and write lock: pass.
- Unit suite: 2,021 passed on 3.14, 2,019 on 3.14t. Conformance against oxide: 506 and 508.
- `scripts/check.sh` on 3.14, 3.14t, 3.15 and 3.15t: pass.
- `bench.gate` against diskcache 5.6.3: GO on 3.14 (`get_hit_small` 0.49x, `set_small` 0.77x) and on 3.14t (0.54x, 0.76x). The first 3.14t run missed the 0.55x read target by rounding on a loaded machine; the rerun passed.

In progress or not yet run: the full sanitizer lanes (running now), the 24-hour soak (starts on this machine when the sanitizers are green), the Linux container. Nothing has run on Windows or on x86-64. Treat what depends on those as unverified.

## How to build and run

From your clone. `CLAUDE.md` has the full list.

```bash
bash -c 'source scripts/uv_env.sh && dco_env venv 3.14'       # venv/ with locked deps and the project, editable
bash -c 'source scripts/uv_env.sh && dco_env .venv-ft 3.14t'  # the free-threaded build
cargo test
venv/bin/pytest tests/unit                                # oxide-only tests
venv/bin/pytest --backend=oxide tests/conformance         # plain `pytest` tests diskcache, not oxide
scripts/model-check.sh                                    # the shuttle models
```

Never `uv sync`. The fd-heavy tests need `ulimit -n 10240`. Each fork scenario runs in its own subprocess through `python -m tests.unit.<module> <scenario> <directory>`, which is the easiest way to write a reproduction. This machine is shared: run heavy jobs under `nice -n 20`, leave six cores free, and don't run benchmarks while the soak is going (its numbers and yours would both be skewed).

## Part 1: the review

One Markdown file, `reviews/deep-review-fbcc45ff-<date you finish>.md`, in the format of your last one: a one-paragraph verdict with counts by severity, then each finding with severity, where, trigger, effect, evidence, confidence and a fix idea, with a runnable reproduction where you have one. For each finding, say whether you reproduced it or traced it line by line. Include your verdict on each of the four known limits, and end with what you checked and found sound. Write the review before you change any code, and commit it on its own.

## Part 2: the fixes

For each finding you reproduced or traced, in severity order:

- Write the failing test first, see it fail on `fbcc45ff`, then fix. One commit per finding or per tightly related group. The commit message is one descriptive paragraph on a single line saying what changed and why (`git log` shows the style), with no attribution or co-author lines.
- Follow the conventions in `CLAUDE.md`: `Optional[X]` never `X | None`, no leading underscore on new names, clippy pedantic with no `unwrap` in production Rust, ruff at 120 columns with single quotes, Markdown never hard-wrapped.
- Every behaviour claim in the docs must match the code. When a behaviour changes, update `CHANGELOG.md` (Unreleased), `tests/DIVERGENCES.md`, the affected pages under `docs-source/`, the skill and the docstrings in `_core.pyi`, then rebuild the site with `make docs` and commit `site/`.
- A step is done when `cargo clippy`, `cargo test` and the unit and oxide conformance suites pass on both 3.14 (`venv/`) and 3.14t (`.venv-ft/`). Run `scripts/model-check.sh` after any change to `gate.rs` or the write lock.
- Add a dated entry to the Log in `plans/32-fork-lock-records.md` for what you found and fixed, and for anything you chose to leave, with the reason.

Stop and report instead of deciding when a fix would change public behaviour in a way the docs don't already describe, need a new divergence (`D-NNN`), or change what is written to disk. Those are the owner's decisions.

Out of scope: the version bump to 0.5.8, the changelog date, wheels, tags, PyPI, pushing, force-pushing and the pull request's state. Don't change the on-disk format.

## What to hand back

The review file, the list of commits in your clone with one line each, the test and lint results on your final commit, anything you stopped on for a decision, and a plain statement of what you could not verify. Any change to shipped code means the full sanitizer lanes and the 24-hour soak run again on the final commit, so say clearly whether your commits touch shipped code or only tests and docs.
