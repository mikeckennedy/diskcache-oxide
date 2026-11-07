# Plan 17: empty value-file directories

Status: draft, for the architect's review. Work starts after the htz soak ends (2026-11-07).

## 1. The problem

A value of 64 KiB or more lives in its own file at `aa/bb/<32 hex>.val` under the cache directory (`spill.rs`). `PendingFile::write` creates `aa/bb` with a recursive `DirBuilder` as needed, and nothing ever removes a directory: `Store::unlink`, `PendingFile::discard` and `write_new`'s cleanup remove only files. So every `aa/bb` pair a store has ever used stays, empty or not.

Measured on the htz soak (x86-64, ext4, 3.65 h in, 2026-11-06):

| Store | Directories | Empty | Files |
|---|---|---|---|
| fanout (8 shards) | 230,339 | 228,315 | 69 |
| durable | 64,319 | 63,934 | 132 |
| behind | 31,200 | 30,940 | 6 |
| chaos | 18,636 | 18,279 | 105 |
| ledger | 15,796 | 15,515 | 27 |

The fanout store was 959 MiB on disk for about 40 MiB of live data. The ceiling is 256 + 256 × 256 = 65,792 directories per store (each FanoutCache shard is a store): about 257 MiB at 4 KiB a directory, and 65,792 inodes. A default FanoutCache reaches about 2 GiB and 526,000 inodes. It takes nothing unusual to get there: a few hundred thousand large-value writes or deletes over a cache's life, which Talk Python's caches will see. On APFS directories are cheaper in bytes, but the inode and directory-entry cost is the same.

## 2. What diskcache 5.6.3 does

diskcache removes them. `Disk.remove(file_path)` (core.py:312) unlinks the file, then calls `os.removedirs(full_dir)`, both under `suppress(OSError)`: `removedirs` removes `aa/bb`, then `aa`, stopping at the first that isn't empty (the cache directory always holds the database, so it stops there). `Cache.check(fix=True)` also walks the tree and `rmdir`s every empty directory (core.py:1979). The write side copes with the race this opens: `Disk.store` loops up to 10 times over `os.makedirs(full_dir)` then `open(full_path)`, because "another cache may have deleted the directory before the file could be opened" (core.py:233).

So this is a fix, not a divergence: oxide differs from the oracle today, untested. No D-entry exists for it, and no conformance test looks at directories after a delete.

## 3. Options

**A. Remove a directory when its last file goes (diskcache's way).** After unlinking a value file, `rmdir` its `aa/bb`, then `aa`; `ENOTEMPTY`, `ENOENT` and `EEXIST` are the expected answers and are ignored. The writer retries its directory create and temp-file create on `ENOENT`. Directories then track the live files: at most two per value file, usually far fewer.

**B. One level of directories instead of two (`aa/<hex>.val`).** At most 256 directories, about 1 MiB, ever, with no new race: nothing is removed. Existing rows keep working, since `filename` stores the relative path and reads join whatever is stored. The cost is entries per directory: a million value files is about 3,900 per directory, which ext4's htree and APFS handle without trouble; diskcache's two levels were for "older filesystems".

**C. Both.** One level, and remove the leaf when it empties.

## 4. Recommendation: A, keeping two levels

A matches the oracle exactly, so behavior and on-disk shape agree with diskcache and the conformance suite can pin it on both backends. It bounds disk use by the live files rather than by a constant, and it keeps the layout every existing cache already has (no format change, no mixed layouts to reason about). B is simpler and race-free, but leaves oxide different from diskcache in a test-visible way (empty directories remain after a delete) and needs a D-entry for what would otherwise be a plain fix. C adds B's format change to A's work for no gain A doesn't already give.

The race A opens is narrow and has a known shape (diskcache has lived with it for years); section 5 makes the writer's side airtight rather than "retry 10 times".

## 5. Design (option A)

### 5.1 Removal

A new `spill::prune_dirs(directory, relative)` takes a value file's relative path (`aa/bb/<hex>.val`) and tries `rmdir(directory/aa/bb)`, then, only if that succeeded, `rmdir(directory/aa)`. Every error is ignored: `ENOTEMPTY`/`EEXIST` (another file lives there), `ENOENT` (someone else pruned it), anything else (a permissions oddity must never fail a `delete`). `rmdir` only ever removes an empty directory, atomically, so it can never lose a file. It never goes above `aa`, and never touches the cache directory itself, unlike `os.removedirs`.

Called from every place a value file is removed:

- `Store::unlink` (deletes, overwrites' old versions, `pop`, `cull`, `expire`, `clear`, eviction, the end of a held transaction, a rewound savepoint's orphans). For a batch it collects the distinct `aa/bb` parents and prunes each once after all the unlinks, inside the same detached `gate::run_blocking` section when the batch is detached.
- `PendingFile::discard` (a write that didn't commit) and `write_new`'s failure cleanup.

Cost: one extra `rmdir` per removed value file, which fails fast with `ENOTEMPTY` when the directory has other files. Value files are 64 KiB or more, so the unlink and the write that made the file dominate. Measured in step 4, not assumed.

### 5.2 The writer's race

The interleaving: writer W creates `aa/bb` (or finds it), then pruner P, having unlinked the last file in it, `rmdir`s `aa/bb` before W creates its temp file. W's `create_new` then fails with `ENOENT`. Once W's `.tmp` exists the directory isn't empty, so P can't remove it; the rename to `.val` stays inside the same directory, so nothing after the temp file's creation is exposed.

So only one step needs the retry: "ensure `aa` and `aa/bb` exist, then `create_new` the temp file". `PendingFile::write` loops on `ENOENT` from either the `mkdir` of `aa/bb` (P removed `aa` in between) or `create_new`, re-creating both levels each time. Each round makes progress unless a pruner wins the window again; the window is two syscalls wide. Bound: 64 attempts, then the `ENOENT` is returned as the write's `OSError` (with the path), a far-fetched outcome that the tests in step 2 show never happens in practice. No sleep: the retry is immediate.

Directory creation becomes non-recursive and relative to the cache directory: `mkdir(directory/aa, 0700)` then `mkdir(directory/aa/bb, 0700)`, each accepting `AlreadyExists` only for a directory (as `open::ensure_shard_directory` does). If the cache directory itself is gone, `mkdir(aa)` fails with `ENOENT` every round and the write fails after the bound, instead of silently re-creating the tree. That also closes plan 15's known note: a process still holding a shard open after its root was deleted no longer re-creates `root/000/aa/bb`. To avoid spinning 64 times on a deleted root, `ENOENT` from `mkdir(aa)` is checked once against the cache directory: if it's gone, fail at once.

`commit_name`'s rename and the read path don't change: a rename within one directory can't lose to `rmdir`, and a reader whose file was unlinked already treats `ENOENT` as "overwritten or gone" (D-013, D-016).

### 5.3 Fork and the gate

`rmdir` and `mkdir` take no locks of ours and hold nothing across a `fork()`. Pruning runs where the unlink runs, so it inherits the existing gate rules: detached batches stay inside `gate::run_blocking`, attached single unlinks stay attached (section 5.1's cost is microseconds). No new state, so nothing for `model-check.sh` to model.

### 5.4 Existing caches

Directories that are already empty stay empty until a file in them is written and removed again. Options for the leftovers, for Michael to choose (section 8):

1. Nothing: they drain slowly as pairs are reused; delete the folder on a deploy to start clean.
2. `clear()` also prunes every empty `aa/bb` and `aa` (it already walks every row; a directory walk of 65,792 entries is tens of milliseconds, detached).
3. A one-time sweep at open, recorded in the settings table so it runs once per cache.

Recommendation: 2. `clear()` is already the "empty this cache" call, a sweep there is cheap relative to what `clear()` does, and it gives Talk Python a way to reclaim the space without a restart. Not 3: an open must stay cheap and predictable, and a sweep there races other processes' writers for no benefit over 2.

## 6. Steps

1. **Conformance tests first** (`tests/conformance/test_value_files.py` or the existing large-value module): after `delete`, `pop`, an overwrite with a small value, `cull`, `expire`, `clear`, and a rolled-back `transact`, the cache directory holds no empty directory and no `aa/bb` without a file. They pass on diskcache, fail on oxide today. Same for FanoutCache shards (diskcache's FanoutCache shards are Caches, so the oracle covers them).
2. **Core.** `prune_dirs`, the batch pruning in `Store::unlink`, `discard`'s pruning, and the retrying non-recursive create in `PendingFile::write`. Unit tests: prune stops at `aa`, never removes the cache directory, ignores every error; a writer racing a pruner on one forced `aa/bb` (a test-only hook fixes the hex prefix so every write and delete share one directory) across 8 threads for a few seconds, with no write failing and no directory left empty at the end; a deleted cache directory fails a large write with `OSError` at once and creates nothing.
3. **Python stress.** A unit test under `tests/unit/` with 4 processes and threads writing and deleting large values under one forced prefix, on 3.14 and 3.14t, checking every value read back and the final tree. Marked slow if it runs past a few seconds.
4. **Measure.** `bench.gate` for the large-value delete and overwrite paths, before and after, 3.14 and 3.14t. The bar: no regression beyond noise on the headline 99:1 mix; the large-value delete may cost one syscall more.
5. **`clear()` sweep** (if Michael picks section 5.4's option 2), with a test that an old-style tree full of empty directories is clean after `clear()` and that a concurrent writer's directory survives it.
6. **Soak.** The file scan in `soak/soak.py` counts empty value directories per store and reports them in `status.txt`; a count that keeps growing over hours is a PROBLEM. The next soak runs with it.
7. **Docs.** CHANGELOG (Fixed: value-file directories are removed when their last file goes, as in diskcache, so a long-lived cache no longer accumulates up to 65,792 empty directories per store). Mention in the user guide where value files are described, if anywhere. `make docs`.

## 7. Risks

- **A writer loses the race 64 times.** Requires a pruner to empty and remove the same `aa/bb` within a two-syscall window 64 times in a row. Step 2's forced-prefix test is far harsher than real traffic (one directory instead of 65,536) and must show zero failures.
- **Filesystems where `rmdir` on a non-empty directory is slow or odd** (network filesystems). Out of scope: SQLite in WAL mode already rules out network filesystems.
- **Cost on a delete-heavy workload of large values.** Step 4 measures it.

## 8. Decisions for Michael

1. Option A, keeping two levels (recommended), or B or C.
2. Existing empty directories: nothing, the `clear()` sweep (recommended), or a sweep at open.

## 9. Review

2026-11-06, the architect, on the draft: **PASS with required changes.** Option A is the right call, the core race argument holds (an `rmdir` can only remove an empty directory, and a `.tmp` or `.val` keeps it non-empty from `create_new` until the owner's unlink), and the placement under the gate is right. The changes below tighten the create loop, close two leaks, protect `rmdir` from a hostile `filename`, make the tests deterministic, and account for one behavior change and one oracle bug the draft didn't mention.

Verified against the code: every removal goes through `Store::unlink` (store.rs:903; callers store.rs:374, 885, 894, 1646, ops.rs:245, and through `write_files` for delete, pop, cull, expire, clear, incr over an expired large value, eviction) or `settle_with`'s `discard` (ops.rs:502). `touch` touches no file (ops.rs:411). There's no `push`/`pull` or `check()` in oxide, so nothing else to cover. Each FanoutCache shard is its own `Store` with its own directory, so shards come for free; fanout.rs's temp file lives in the root and is unaffected.

### Option A vs B vs C

A, agreed. B fixes nothing for the trees that already exist, adds a second layout forever, and needs a D-entry; C pays for both. One wording fix: A doesn't match diskcache *exactly*. `os.removedirs` climbs past `aa` (it stops at the cache directory only because the database is there), `os.makedirs` is recursive (diskcache re-creates a deleted root), and diskcache leaks a value file written inside a rolled-back `transact()` (verified on 5.6.3: `with c.transact(): c.set('k', big); raise` leaves `65/fa/<hex>.val` with no row, because `_transact` removes `filenames` only on commit, core.py:732-747). Say "matches diskcache's directory removal" instead.

### Race analysis (5.2): correct, with these confirmations and gaps

- Rename `.tmp` to `.val`: safe, same directory, and the `.tmp` keeps it non-empty. `commit_name`'s `NotFound && val.exists()` retry (spill.rs:68) is unaffected. That only holds while nothing but the owner removes a `.tmp`, so the 5.4 sweep must never remove files, only `rmdir`.
- Held transactions with files written but rows uncommitted: the file exists, so the directory can't go. Savepoint rewinds (store.rs:880-886), doomed rollbacks and the abandoned-`Lend` path (store.rs:374) all go through `Store::unlink`, so they prune too.
- A reader that has opened the file (the D-016 fallback, ops.rs:213-230) keeps its fd through unlink and `rmdir`. One that hasn't gets `NotFound` from `open` whether the file, `aa/bb` or `aa` is gone, so `spill::read`'s `NotFound` mapping (spill.rs:96) covers it, and names are never reused, so a re-created directory can't make an old name reappear.
- Pruning `aa` while a writer has made `aa` but not `aa/bb`: the writer's `mkdir(aa/bb)` gets `ENOENT` and retries. Covered, provided the loop restarts at `mkdir(aa)`.
- Fork children and other processes: `rmdir`/`mkdir` are filesystem-atomic and hold no state of ours, so a child or another process is just one more pruner. A child that forgets an inherited transaction's files (store.rs:1751) also doesn't prune, which is right.
- Gap, the `EEXIST` check is a TOCTOU (required change 1). Gap, a failed create leaks directories (required change 3). Gap, the deleted-root check is redundant (required change 2). Gap, mixed-version deploys (required change 7).

### Gate and fork placement (5.3): right

Pruning in the same closure as the unlinks, attached below `UNLINK_DETACH_AT` and inside `run_blocking` at or above it, is exactly right. `run_blocking` already allows bounded file work with a level held (gate.rs:17-21, 98-101), and unlinks run after the write lock is released (store.rs:893-894, 811-816). The 64-round create loop belongs inside `PendingFile::write`'s existing `run_blocking` closure (spill.rs:47). One addition: the cost model in 5.1 is wrong for the common case (required change 8).

### Required changes

1. **Don't `stat` after `EEXIST`.** 5.2 says each `mkdir` accepts `AlreadyExists` "only for a directory (as `open::ensure_shard_directory` does)", which is `directory.is_dir()` (open.rs:177). Here that's a race: `mkdir(aa)` gets `EEXIST`, a pruner removes `aa`, `is_dir()` is false, and the write fails hard instead of retrying. Treat `EEXIST` as "go on to the next step" with no `stat`: if `aa` is really a file, `mkdir(aa/bb)` fails `ENOTDIR`, and if `aa/bb` is a file, `create_new` fails `ENOTDIR`. Both are terminal errors. Only `ENOENT` from `mkdir(aa/bb)` or `create_new` retries. That's one syscall fewer and no window.
2. **`ENOENT` from `mkdir(directory/aa)` is terminal, with no check.** With a non-recursive `mkdir`, that error means the cache directory (or a parent) is missing. Nothing in oxide ever removes the cache directory, so there's nothing to retry and nothing to check, and a `stat` of the directory would be its own TOCTOU. Drop "checked once against the cache directory" and fail at once with `Error::Io` naming the path. A root deleted mid-loop still ends in one round, because the next `mkdir(aa)` fails.
3. **Prune on every failed create, not just `write_new`'s.** If `create_new` fails with anything but `ENOENT` (`EMFILE`, `ENOSPC`, `EACCES`, `EDQUOT`), the directories this call just made stay empty forever. If `mkdir(aa/bb)` fails, `aa` does. Likewise after the 64th `ENOENT`. `PendingFile::write` should call `prune_dirs` on every error path after its first `mkdir`, plus the `remove_file` `write_new` already does (spill.rs:85). Put it in the caller, which knows `directory` and `relative`, rather than in `write_new`, which only has a path.
4. **`prune_dirs` must validate `relative` before removing anything.** `filename` comes from the database, and `directory.join(relative)` follows absolute paths and `..` (`Path::join("/x")` replaces the base). Today a damaged row can make `unlink` remove a file outside the cache. With pruning it could also `rmdir` empty directories anywhere, `/tmp/foo` for a `filename` of `/tmp/foo/x.val`. Parse it as exactly `[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{32}.(val|tmp)`, build the two directory paths from those components (`directory.join(aa)`, `directory.join(aa).join(bb)`), and prune nothing otherwise. Return `()`, with no error path. Hardening `unlink` the same way is worth a follow-up (suggestion 1), but the prune must not inherit that hole.
5. **Deterministic tests for the retry, not only a timing race.** The 8-thread forced-prefix test is good for confidence but probabilistic: a "no retry" mutation could pass it on a quiet run. Add a `#[cfg(test)]` thread-local injection point between `mkdir(aa/bb)` and `create_new`, in the style of `FAIL_NEXT_END_BUSY` (store.rs:1803). When armed with `n`, it `rmdir`s `aa/bb` (and `aa`) the next `n` times. Assert: `n = 1` succeeds, the re-created directories are 0700 (D-015 is only tested on first creation today, spill.rs:243-251), `n = 63` succeeds, `n = 64` fails with `OSError` naming the path and leaves no empty directory (catches change 3), and a deleted root fails after exactly one `mkdir` (catches a spin, change 2) and creates nothing (catches "recursive create kept"). For the forced prefix, make the hex a parameter rather than a global in core: a `pub(crate) fn write_named(directory, hex, data)` that `write` calls with `random_hex()`, so core tests need no static at all.
6. **Cover the cases the step-1 list misses, and a batch that shares a directory.** Add `add` of a large value on an existing key (the `discard` path, ops.rs:311-325), an overwrite large to large, `incr` over an expired large value, `cull` through `size_limit` on `set`, and a held `transact()` that commits overwrites. In core, use the forced prefix to put two or more files in one `aa/bb` within a single unlink batch, both below and at or above `UNLINK_DETACH_AT` (8). Otherwise a mutation that prunes after the first file of a directory, or dedupes wrongly, passes every test, since random prefixes almost never share a directory. "Prune before unlink" is already caught by the plain `delete` test (a single file, so its directory is never empty at prune time). For the rolled-back `transact()` case, assert only "no empty directory" across backends: on diskcache it passes vacuously because of the leaked file. Assert "no value file and no value directory left" oxide-only, with whatever marker the conftest requires, and log the diskcache leak in `upstream/README.md` with a repro in `upstream/repros/` (convention). The four-line repro is in the review notes above.
7. **Record two behavior consequences.** (a) After the root is deleted under a live process, a large `set` now raises `OSError`, while a small `set` still succeeds into the unlinked database, and diskcache's `makedirs` re-creates the tree and succeeds. That's test-visible against the oracle, so it needs a `D-NNN` entry (or say it's out of the conformance surface and note it in D-032/D-038's neighborhood). Update plan 15 section 10's note to point here. (b) Mixed-version deploys: an old oxide writer (recursive create, no retry, spill.rs:49-50) beside a new pruner, or the 5.4 sweep, can lose the window and fail one `set` with `OSError`. That's vanishingly rare (same `aa/bb`, same few microseconds) but real during Talk Python's rolling deploys, so list it in section 7.
8. **Measure the sparse case, where pruning succeeds.** 5.1 assumes `rmdir` "fails fast with `ENOTEMPTY`", but a live cache with about 100 value files across 65,536 directories almost never shares one. The common case is that every large delete or overwrite *succeeds* at two `rmdir`s, and the next large write pays two `mkdir`s: four journaled directory updates per cycle, versus none today. `bench.gate`'s headline workloads don't spill at all (bench/gate.py:29-35), so "no regression on 99:1" is vacuous. Measure `bench.baseline`'s `set_page_entry_450k` (an overwrite: new file, old one unlinked) and a large set-then-delete loop on a near-empty cache, at 1 and 8 threads, 3.14 and 3.14t, against diskcache (which pays the same `makedirs`/`removedirs`). The bar should be "within diskcache's cost for the same cycle".
9. **Pin the 5.4 sweep's constraints if Michael picks option 2.** It only `rmdir`s, never removes a file (a `.tmp` may be another process's in-flight write, see the rename note above). It matches names exactly `[0-9a-f]{2}` at both levels, so it never touches a FanoutCache shard (`000`) or the user's own folders. It runs in the shard's directory, never the root. It runs after the chunk loop finishes, with no write lock and no gate level, through `run_blocking`, and only when `GATE.own_depth() == 0`. Inside a held transaction, skip it: the clear's files go at commit and prune there, and a 65,792-entry walk while holding a level would make a pending fork wait tens of milliseconds. It's best-effort, never changes `clear()`'s return or raises, and is skipped when `clear_until` was `Interrupted`. Against new-code writers in other processes it's safe: it's one more pruner, visiting each directory once, so it costs a racing writer at most two of its 64 rounds.

### On 5.4's recommendation

Option 2 is right, with change 9's constraints. It's safe against concurrent writers for the same reason pruning is (`rmdir` can't lose a file, and the writer retries), its cost shrinks to almost nothing once a tree is clean (after the fix, only directories with live files exist), and `clear()` is the only "make this cache empty" call oxide has, since there's no `check()`. Not option 3, for the reasons given. Option 1 is a fair fallback: the installed population is small (Talk Python and the soaks), and plan 15 already makes "delete the folder with every worker stopped" the documented reset.

### Optional suggestions

1. Harden `Store::unlink` and `spill::read`/`open` with the same `filename` validation as change 4, as a separate small change. A damaged row should never reach outside the cache directory. Today `directory.join` lets it (store.rs:904, spill.rs:93).
2. Do the batch prune in two passes: every distinct `aa/bb`, then every distinct `aa` whose `bb` was removed. It reads more clearly than per-parent "then maybe `aa`", and it's the same syscalls.
3. For the Python stress test (step 3), prefer an adversary process that loops `os.rmdir` over the forced directory (always safe, since it removes only empties) to a forced-prefix knob alone. If a knob is still needed, follow the `_sqlite_error`/`_gate_stats` precedent (crates/diskcache-oxide-py/src/lib.rs:223-304): a private `_core._force_value_prefix(prefix: str | None)` that validates four hex characters, is listed only under the private names in `_core.pyi`, is process-wide (forked children inherit it, spawned ones must call it), and is documented as test-only. In production it could only cost performance, never correctness. No environment variable (CLAUDE.md: the library has none).
4. Use `ErrorKind::DirectoryNotEmpty` (stable) in comments and tests rather than raw errno. POSIX allows `rmdir` to return `EEXIST` for a non-empty directory, and since every prune error is ignored, nothing needs to branch on it.
5. Soak step 6: also count `aa` directories with no `bb`, and `.tmp` files older than an hour, so a leak on either path shows up.

## 10. Progress

(not started)
