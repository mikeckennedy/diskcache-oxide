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

(pending)

## 10. Progress

(not started)
