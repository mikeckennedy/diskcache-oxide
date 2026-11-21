# Handoff: follow-up review of plan 32 (the fork fixes, 0.5.8)

You reviewed diskcache-oxide 0.4.6 on 2026-11-19. That review ended with two fork bugs that needed design, a checkpoint fix and several smaller findings. They were fixed together on one branch, and this is a request to review those fixes before they ship as 0.5.8. Try to break them. A finding that the fix is wrong, incomplete, or opened a new hole is worth more than confirmation that it works.

## What to review

- Repository: `diskcache-oxide`, branch `plan-32-fork-lock-records` (draft PR #7).
- Revision: `03ad2038`. Base: `main` at `2319ce3f` (0.4.7 plus a build-script change).
- The diff: `git diff main...03ad2038 -- . ':!site'`. `site/` is the generated docs site; ignore it. Without it the change is about 3,400 lines added across 54 files.
- Work from a snapshot, not the working tree: `git archive 03ad2038 | tar -x -C <a new directory>`. The working tree is in use by long test runs and will change under you.
- Read first: `CLAUDE.md` (layout, commands, conventions, release tiers), then `plans/32-fork-lock-records.md`. The plan's Design section says what was intended; its Log, in date order, says what was built, what each internal review found, and what was deliberately left. `tests/DIVERGENCES.md` entries D-013, D-020, D-050 and D-064 are the public statement of the new behaviour.

## Background you need

SQLite keeps a per-process record of the file locks it holds on each database file. That record crosses `fork()`. The kernel's `fcntl` locks behind it do not. So a child that inherits an open connection believes it holds locks it doesn't: its reads can go stale and its writes can be lost once the parent closes, with no error and nothing for `check()` to find. Before this branch a child forked inside a transaction could still read, and a child forked from a decode hook inside a cache call could read and write.

A connection that never runs a statement (the "pin" connection) is harmless to inherit.

## What changed, by area

**A. A child that inherited an open connection can't use that cache.** One level, "unusable", replaces the old "unwritable".

- `crates/diskcache-oxide-core/src/poison.rs`: the registry of unusable database files (`check_usable`, `mark_unusable`, `mark_all_unusable`, `take_newly_unusable`).
- `crates/diskcache-oxide-core/src/pool.rs`: `files_with_connections()` scans every pool in the child, using `try_lock` only, and `poison_inherited` marks each file a connection was inherited open to. A connection given back between the fork hooks is closed instead of kept idle (`gate::forking_in_parent`).
- `crates/diskcache-oxide-core/src/gate.rs`, `end_fork_child`: the order is generation bump, `poison_live_holds`, `pool::poison_inherited`, `store::reset_held_in_child`, the gate reset, then `resume_after_fork` (which may start the maintenance thread). The child-side reset also runs early, from the forking thread's first gate entry at any depth, when another library's `after_in_child` hook uses a cache before ours has run.
- `crates/diskcache-oxide-core/src/store.rs`: `check_usable` in `lease`, `lookup`, the slow open and `on_pinned`; `layout.rs` checks in `read_format`.
- `crates/diskcache-oxide-core/tests/unhooked_fork.rs`: a raw `fork()` whose before-hook never ran.

**B. A gate level stranded on a thread the child doesn't have.** If thread B was ending thread A's transaction while A forked, the child kept a level nothing could release, and a later `os.fork()` from another thread of the child hung.

- `gate.rs`: `HeldShare` carries the gate epoch it was made in. `reset_in_child` bumps the epoch, so every share made before the fork enters as inherited and drops without touching a count.
- `crates/diskcache-oxide-core/src/gate/model.rs`: shuttle model 22.
- `crates/diskcache-oxide-py/src/cache.rs`, `Transaction`: the `ending_in` atomic and `end_lost_to_fork()`, read before the state mutex in `__exit__`, `__enter__` and `__repr__`, because that mutex may have been inherited locked.

**C. The background checkpoint.** `wal::passive_copied_everything` no longer reads SQLite's "another connection is checkpointing" row `(1, -1, -1)` as "everything is copied" (`store.rs`, `background_checkpoint`).

**D. Smaller fixes.**

- D1, damaged columns: `rows.rs` reads `filename` and `size` leniently (`lenient_filename`, `lenient_size`, `file_size`). `check.rs` gains a first pass, `check_column_types`, which reports a row with a wrong-typed `mode`, `size`, `store_seq`, `expire_time` or `filename`, and with `fix` removes it by `id` after re-checking that it is still damaged (`RowWriter::delete_damaged`).
- D2: `incr` and `decr` refuse an infinity or NaN (D-064).
- D3: `check(fix=True)` removes a file no row names only under the write lock.
- D4: a `types=` registry on a handle no longer lets a globally registered class be stored under a name the handle maps to another class (`codec/registry.rs`, `Snapshot::by_type`).
- D5: `vacuum` and the checkpoint after it share one deadline.
- D6: `stack.rs`, the lock-free stack: `TakeAllStack`'s marker and an explicit `unsafe impl Sync` with a compile-time assertion.

**Step 8, added on 2026-11-20 after the owner's decisions.**

- `ForkedInsideTransactionError`, a new public `RuntimeError` subclass (`crates/diskcache-oxide-py/src/lib.rs`). `Error::Unusable` and `Error::ForkPoisoned` both map to it. `python/diskcache_oxide/caches.py` (`_resolve`) and `djangocache.py` (`open_handle`) re-raise it instead of serving a `NullCache`. `cache.rs` `classify` and `codec/mod.rs` `passes_through` let it pass through a decode hook as itself, so a hook that reads an unusable cache doesn't get the row it was decoding deleted.
- `store_seq` numbering ignores damaged rows. `rows.rs` has two SQL macros: `max_store_seq!` (the largest `store_seq` below 2^62, one seek on the `cache_store_seq` index) and `next_store_seq!` (`NULLIF(MIN(MAX(max + 1, floor), 2^62), 2^62)`, so a write that would reach the ceiling fails on `NOT NULL` instead of repeating a number). Five statements use them: the upsert, `UPDATE_NUMBER_SQL`, `raise_seq_floor`, and `seq_snapshot` in `maintenance.rs`. `WRONG_TYPE` counts a `store_seq` of 2^62 or more as damage.

## Claims to attack

These are what the branch asserts. Each is a place to look for a counter-example.

1. In a forked child, no code path runs a SQL statement on a connection opened before the fork, and no path opens a new connection to a file marked unusable. Every production open is behind `check_usable` under a gate level.
2. The pool scan in the child is complete: every way a connection can be open at the fork (checked out, idle, held by a transaction, held by a parked forker or a parked end, the maintenance thread's) is either closed before the fork or found by the scan. It uses `try_lock` only, so it can't deadlock on a mutex inherited locked; ask what it concludes when a `try_lock` fails.
3. The child never closes a file descriptor the parent's connection still uses, and never unlinks or truncates a file on the parent's behalf.
4. After `reset_in_child`, the gate's holder count equals the forking thread's own guard depth, whatever other threads were doing at the instant of the fork, including a thread half-way through `try_enter` or `HeldShare::drop`.
5. `Transaction.__exit__`, `__enter__` and `__repr__` in a child never touch a mutex another thread held at the fork.
6. On a `FanoutCache`, only the shard that was open is refused, and a call that spans every shard raises before it changes any.
7. `store_seq` never repeats among sound rows (D-011), for any single damaged cell in `cache.store_seq` or `state.next_seq`, and for several at once. `clear()` and `evict(tag)` terminate and skip no sound row.
8. `check(fix=True)` never deletes a row that an overwrite made healthy between its read and its delete, and never raises on what it finds.
9. A lenient read of a damaged `filename` can't be turned into a path outside the cache directory.
10. The new exception class reaches the caller unchanged through `durable=True` handles, the `Caches` registry, the Django backend and decode hooks, and nothing still converts it into a miss, a `NullCache` or a deleted row.

## Where I'd look hardest

- The window between `fork()` returning in the child and `end_fork_child` finishing, with another library's fork hooks registered before and after ours (`tests/unit/fork_inherited_scenarios.py` shows how the tests stage this with `LATE_BEFORE` and `EARLY_CHILD`).
- Two threads calling `os.fork()` at once, one of them inside a transaction or a cache call. The second parks, the first fork doesn't wait for a parked thread, and the first child inherits the second thread's connection. The tests cover this (`transact_inherited_mid_operation` in `tests/unit/fork_scenarios.py`); the docs gained it only today.
- Free-threaded Python (3.14t, 3.15t), where the GC can finalize a `Transaction` on any thread and `os.fork()` stops the world.
- The maintenance thread: stopped and joined before every fork, restarted after it in parent and child. Anything it does between its restart and the child's first call.
- The `store_seq` SQL on the bundled SQLite (3.54.1, flags in `.cargo/config.toml`), including the upsert's `ON CONFLICT` path and whether the min/max optimization really applies to each of the five statements, not just the bare fragment the test measures.
- The `unsafe` in `stack.rs` and `sqlite_mutex`: only `stack.rs` changed, by a marker type and an `unsafe impl Sync`.

## Already known: don't re-report these unless you find they're worse than stated

- A read on an unusable cache while another thread of the child has a fork pending waits for that fork before it raises (writes check first and are immediate).
- If another library's `after_in_child` hook, registered before ours, calls `clear()` on a `FanoutCache` in a child forked inside `transact(key=)`, the usable shards can be cleared before the call raises.
- A hook that runs with no connection out (encoding for `set`, decoding a value stored in a file) and forks leaves the child a working cache. That is intended and safe.
- A row with a damaged `mode`, `expire_time` or `store_seq` still raises `OSError` from calls on its own key, and from `pull`, `peek` and `peekitem` when it's the row they reach, until `check(fix=True)`.
- A `store_seq` just under 2^62, or a damaged `state.next_seq`, makes writes fail loudly, and `check(fix=True)` doesn't repair the floor.
- A row whose `size` was damaged leaves `state.size` off until `check(fix=True)` recomputes it.
- A lookup of a disabled, never-opened cache in a `Caches` registry raises in such a child, and the registry's admin methods (`stats`, `expire_all`, `remove_old_formats`) report the error per cache rather than raise.
- On macOS, forking in a tight loop occasionally draws CPython's "multi-threaded fork" `DeprecationWarning`: the wait for the stopped maintenance thread to be reaped is Linux-only. Seen once in 300 forks. Plan 28 behaviour, not changed here; say so if you think it matters more than that.
- The docs site's version badge still says v0.4.7 and `Cargo.toml` still says 0.4.7. The bump to 0.5.8 is the release commit.

## What has been run

At `03ad2038`, on macOS arm64:

- `cargo clippy --all-targets -- -D warnings`, `cargo test` (496 core lib tests), `cargo doc` with `-D warnings`, ruff and ty: pass.
- Unit suite: 2,010 passed on 3.14, 2,008 on 3.14t. Conformance against oxide: 506 and 508.
- `bench.gate` against diskcache 5.6.3: GO on 3.14 and 3.14t. The 3.14t `get_hit_small` line passed exactly at its 0.55x target on a loaded machine, after missing by 0.01 on an earlier run.

At earlier commits on the branch, before step 8:

- `scripts/check.sh` on 3.14, 3.14t, 3.15 and 3.15t: pass (at `851efcdd`).
- All five sanitizer lanes as a quick pass (Miri, TSan and ASan natively and in a Linux container): pass, after two faults in the branch's own tests were fixed.
- 28 shuttle models of the fork gate and write lock: pass.

Not yet run on this revision: the full sanitizer lanes, `check.sh`, the Linux container, and the 24-hour soak. Treat anything that depends on those as unverified. Nothing here has run on Windows or on x86-64.

## How to build and run

From the snapshot directory. `CLAUDE.md` has the full list.

```bash
bash -c 'source scripts/uv_env.sh && dco_env venv 3.14'       # venv/ with locked deps and the project, editable
bash -c 'source scripts/uv_env.sh && dco_env .venv-ft 3.14t'  # the free-threaded build
cargo test
venv/bin/pytest tests/unit                                # oxide-only tests
venv/bin/pytest --backend=oxide tests/conformance         # plain `pytest` tests diskcache, not oxide
scripts/model-check.sh                                    # the shuttle models
```

Never `uv sync`. The fd-heavy tests need `ulimit -n 10240`. Fork tests live in `tests/unit/fork_scenarios.py`, `fork_inherited_scenarios.py`, `fork_timeout_scenarios.py` and `durable_scenarios.py`; each scenario runs in its own subprocess through `python -m tests.unit.<module> <scenario> <directory>`, which is the easiest way to write a reproduction of your own.

## Ground rules

- Change nothing in the repository. Build, probe and write disposable databases in temporary directories only.
- For each finding, reproduce it or trace it line by line, and say which. A finding with a script that fails is worth far more than one inferred from reading. Say plainly when something is a suspicion you couldn't confirm.
- Report against the reviewed revision, with file and line.
- Don't file style points. The project's conventions (`Optional[X]` not `X | None`, no leading underscores on new names, clippy pedantic, no `unwrap` in production Rust) are checked by tooling.

## What to deliver

One Markdown file, `reviews/deep-review-03ad2038-2026-11-20.md`, in the format of `reviews/deep-review-5b2ea85-2026-10-31.md`: a one-paragraph verdict with counts by severity, then each finding with severity, where, trigger, effect, evidence, confidence and a fix idea, and a runnable reproduction where you have one. End with what you checked and found sound, so the absence of a finding in an area can be told from the area not having been looked at.
