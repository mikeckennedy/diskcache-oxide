# Spike 4a: storage experiments (results)

Phase 1, Spike 4a, run 2026-10-28. Throwaway code in `spikes/storage/` (its own Cargo workspace, rusqlite 0.41.0 bundled = SQLite 3.54.1, Rust 1.99.0). Machine: Apple M5 Pro, 18 cores, macOS (Darwin 27.2), AC power. Every cross-process experiment runs as separate OS processes: each binary in `spikes/storage/src/bin/` re-spawns itself in writer/reader roles. Nothing under `crates/`, `python/`, `tests/` or `bench/` was touched.

How to rerun: `cargo build --release` in `spikes/storage`, then `scripts/exp1.sh` (race matrix, summarized by `scripts/summarize_exp1.py runs/exp1.log`), `target/release/killspill run --dir runs/kill --rounds 100`, `target/release/noopen run --dir runs/noopen`, `scripts/exp4.sh` (clear fairness), `target/release/prefix run --dir runs/prefix`, and `.venv/bin/python -I scripts/diskcache_prefix.py runs/dcprefix 100000 400000` plus `scripts/page_cost.py <cache.db>` for the diskcache side. Raw logs are in `spikes/storage/runs/*.log` (gitignored).

**Headline.** All five experiments met their targets, with two findings that change the design. (1) The optimistic "re-read the row and retry" spill read can livelock when a reader is slower than the overwrite rate, so it needs a bounded fallback (a locked read, below). (2) The writer getting into `clear()` "for free" in our first runs was an accident of WAL auto-checkpoint gaps. With those removed, SQLite's default busy handler lets the writer in only 0/20 times. A short custom poll plus a 50-100 µs pause between chunks is what makes it reliable. Separately, the plan's hypothesis about diskcache's quadratic `iterkeys` is confirmed, but a naive `EXPLAIN QUERY PLAN` on traced SQL hides it.

## Prototype schema and protocol (what was tested)

```sql
CREATE TABLE cache (
    id INTEGER PRIMARY KEY, key TEXT NOT NULL UNIQUE, mode INTEGER NOT NULL,
    value BLOB, filename TEXT, size INTEGER NOT NULL,
    store_time REAL NOT NULL, expire_time REAL, store_seq INTEGER NOT NULL);
CREATE INDEX cache_store_seq ON cache (store_seq);
CREATE INDEX cache_filename ON cache (filename) WHERE filename IS NOT NULL;
CREATE TABLE settings (name TEXT PRIMARY KEY, value) WITHOUT ROWID;
-- PRAGMA user_version = 1
```

`store_seq` is assigned in the upsert as `(SELECT COALESCE(MAX(store_seq), 0) + 1 FROM cache)` inside `BEGIN IMMEDIATE`, served by the `store_seq` index. It is unique per stored version, so `(id, store_seq)` names one version of one row.

**Spilled `set`** (value length ≥ threshold):

1. Pick a random 128-bit name `n` (32 hex chars). The relative path is `n[0:2]/n[2:4]/n.val`; `create_dir_all` the parent.
2. Write the value to `n[0:2]/n[2:4]/n.tmp` (`create_new`, no fsync) with **no lock held**.
3. `BEGIN IMMEDIATE`. Read the old `filename` for the key.
4. `rename(n.tmp → n.val)` **while holding the write lock**.
5. Upsert the row (`filename = 'aa/bb/n.val'`, `value = NULL`, new `store_seq`). `COMMIT`.
6. After the commit, unlink the old `.val` (ENOENT ignored).
7. On any error before the commit: `ROLLBACK`, then unlink both `n.tmp` and `n.val` (ENOENT ignored).

Inline sets and deletes use the same transaction shape: read the old filename, write the row change, commit, then unlink the old file.

**Spilled `get`:**

1. `SELECT id, value, filename, store_seq FROM cache WHERE key = ?` (autocommit, so the read snapshot ends with the statement).
2. Open and read the file with no transaction held. Success is a hit (an fd opened before an unlink still reads the whole file).
3. On ENOENT, re-read the row:
   - row gone: a miss;
   - `filename` or `store_seq` changed: the value was overwritten, so retry from step 2 with the new row;
   - unchanged: a true miss. Run `DELETE FROM cache WHERE id = ? AND store_seq = ?` with `busy_timeout(0)` (BUSY is swallowed), restore the normal busy handler, and return the miss.
4. **After 8 optimistic attempts** (new, see Experiment 1): `BEGIN IMMEDIATE`, read the row, open the file (keep the fd), `COMMIT`, then read from the fd with no lock held. While the write lock is held no writer can commit a replacement, and old files are only unlinked after a commit, so the file the row names is guaranteed to exist. This wait is bounded by the cache's `timeout` and must release the GIL.

## Experiment 1: spilled-value read race (D-016) and compare-and-delete (D-013)

**Setup.** This is Test A's scenario. One key `hot`; 1 writer process overwrites it 3000 times with self-verifying blobs (`key|n|` + random bytes + FNV-64 of the rest) of 2× to 2.5× the threshold. 4 (or 6) reader processes `get` it in a tight loop until the writer finishes, then read once more. Afterwards the orchestrator checks the final value, the row count (1), the `.val` file count (1), and orphans (via the `check()` design below). Thresholds: 32 KiB (64-80 KiB values) and 64 KiB (128-160 KiB values). Two extra knobs:

- `--gap-us N` sleeps between the row read and the file open, to widen the race window on purpose.
- `--naive` is a negative control that reads like diskcache: ENOENT is a miss, with no re-check. `--naive-delete` adds a diskcache-style cleanup, `DELETE ... WHERE key = ?`, on that miss.

**Results** (sums over runs; "retries" counts re-reads after ENOENT with a changed row):

| threshold | readers | variant | runs | reads (hits) | misses | torn | errors | retries (max/read) | locked fallbacks | final value ok, 1 row, 1 file |
|---|---|---|---|---|---|---|---|---|---|---|
| 32 KiB | 4 | protocol | 20 | 772,395 | 0 | 0 | 0 | 23 (1) | 0 | 20/20 |
| 32 KiB | 6 | protocol | 5 | 284,055 | 0 | 0 | 0 | 43 (2) | 0 | 5/5 |
| 32 KiB | 4 | protocol, gap 100 µs | 5 | 57,535 | 0 | 0 | 0 | 14,717 (4) | 0 | 5/5 |
| 32 KiB | 4 | protocol, gap 1 ms | 5 | 3,051 | 0 | 0 | 0 | 8,932 (8) | 793 | 5/5 |
| 64 KiB | 4 | protocol | 20 | 573,840 | 0 | 0 | 0 | 11 (1) | 0 | 20/20 |
| 64 KiB | 6 | protocol | 5 | 222,583 | 0 | 0 | 0 | 10 (1) | 0 | 5/5 |
| 64 KiB | 4 | protocol, gap 100 µs | 5 | 62,682 | 0 | 0 | 0 | 20,040 (2) | 0 | 5/5 |
| 64 KiB | 4 | protocol, gap 1 ms | 5 | 3,738 | 0 | 0 | 0 | 12,656 (8) | 1,105 | 5/5 |
| 32 KiB | 4 | naive control | 10 | 352,607 | 30 (in 5/10 runs) | 0 | 0 | n/a | n/a | 10/10 |
| 64 KiB | 4 | naive control | 10 | 284,278 | 12 (in 6/10 runs) | 0 | 0 | n/a | n/a | 10/10 |
| 32 KiB | 4 | naive control, gap 100 µs | 3 | 34,253 | 11,024 | 0 | 0 | n/a | n/a | 3/3 |

With the protocol, misses and torn reads were zero in every configuration, across 1.98M reads. The naive control reproduces the D-016 failure at about diskcache's rate (roughly 1 miss per 10-30k reads, in about half the runs), so the race window is real and the test is sensitive to it. At gap 0 a retry happens about once per 30-50k reads, and never more than twice in a row.

**The livelock (a design change).** Before the locked fallback existed, the 1 ms-gap run livelocked: every reader exhausted 64 optimistic attempts, because each row it read had been replaced (and its file unlinked) before it could open the file. A spilled overwrite takes about 0.23 ms (below), so any reader slower than the overwrite rate between its row read and its `open()` never wins. In production that means a descheduled thread, a GIL handoff, or a slow filesystem combined with a hot key. With 8 optimistic attempts and then the locked read, the same runs finished with 0 misses and 0 errors (793 and 1,105 reads took the fallback). The fallback never triggered at a natural gap of 0 or at 100 µs.

**Compare-and-delete with deletes mixed in.** The writer deletes the key on about 20% of ops (2,805 and 2,940 deletes over 5 runs per threshold), always ending with a set. Readers see legitimate misses (row gone), but across 20 runs (gap 0 and 100 µs, both thresholds) there were **0 true misses and 0 rows removed by a reader's compare-and-delete**. Every run ended with the final value, 1 row, 1 file and 0 orphans. A reader can only reach the delete when the row it re-read still names the missing file, and the delete is keyed on `(id, store_seq)`, so it can never remove a newer version.

**Negative control for the delete.** The diskcache-style `DELETE ... WHERE key = ?` cleanup deleted **5,638 and 5,396 live rows** in 3 runs per threshold. Each deletion orphaned that version's file (the next set found no old row to unlink), and in 3/3 runs at 64 KiB a reader deleted the writer's final value. This is exactly the hazard D-016 describes, and it confirms the test can detect a reader deleting a live row.

## Experiment 2: SIGKILL mid-spill (Test I)

**Setup.** This is Test I's scenario with more rounds. Each round spawns a writer process that loops forever over 6 keys with spilled blobs (2× to 3× the threshold), optionally deleting a key on 25% of ops. The orchestrator waits for the writer's third set, sleeps a random 0-50 ms (or 0-5 ms), then sends SIGKILL. There were 100 rounds per configuration. Afterwards: `PRAGMA integrity_check` on a fresh connection; open the cache (which must not write, per Experiment 3); read every key; `set` and `get` a new spilled key; count files and orphans.

| threshold | variant | integrity | keys: valid hit / miss / corrupt | new write | live rows | `.val` files | orphan `.val` | orphan `.tmp` |
|---|---|---|---|---|---|---|---|---|
| 32 KiB | overwrite, 0-50 ms | ok | 6 / 0 / 0 | ok | 7 | 32 | 25 | 25 |
| 32 KiB | with deletes | ok | 5 / 1 / 0 | ok | 6 | 34 | 28 | 25 |
| 32 KiB | overwrite, 0-5 ms | ok | 6 / 0 / 0 | ok | 7 | 29 | 22 | 19 |
| 64 KiB | overwrite, 0-50 ms | ok | 6 / 0 / 0 | ok | 7 | 22 | 15 | 17 |
| 64 KiB | with deletes | ok | 5 / 1 / 0 | ok | 6 | 30 | 24 | 22 |
| 64 KiB | overwrite, 0-5 ms | ok | 6 / 0 / 0 | ok | 7 | 21 | 14 | 26 |

(The one miss in the delete runs is a key whose last committed op was a delete, which is correct.) There were no corrupt reads, no true misses (no row ever pointed at a missing file), and no errors on reopen. The reopen never wrote.

**Orphan rate.** About 15-28 orphan `.val` and 17-26 orphan `.tmp` files per 100 kills. Timing the phases of a spilled set (`target/release/setphases`) explains this: at 32 KiB a set takes 231 µs, of which 59 µs is the temp write, 31 µs the old-file unlink, and about 140 µs the locked part (BEGIN, rename, upsert, COMMIT). A kill in the temp-write window leaves a `.tmp`. A kill between the rename and the end of the unlink leaves an orphan `.val`, either the new file (before the commit) or the old one (after it). The writer here does nothing but spill-writes, so this is a worst case. A deploy restart rarely lands mid-set, but orphans accumulate across restarts, so `check()` (Phase 6a) must clean them up.

**Temp-file naming and the `check()` design.** The rules that make orphan detection exact:

- Temp files are `aa/bb/<32 hex>.tmp`, in the same directory as their final name (same filesystem, so the rename is atomic). Live files are `aa/bb/<32 hex>.val`. The name is random and never reused, so an overwrite always changes `filename`, which is what lets a reader detect the overwrite.
- The `.tmp → .val` rename happens only while holding the write lock, and the row is committed before the lock is released. So **while holding the write lock, a `.val` file that no row references is always an orphan**, with no grace period and no age heuristic. A `.tmp` file is never live; it is garbage once its writer is gone. A writer can't be identified from the file, so `check()` deletes a `.tmp` only when it is older than a grace period (suggest max(1 hour, 10× timeout)), because a slow writer may still be filling it.
- `check()` algorithm (prototyped as `find_orphans` and run after every experiment):
  1. List `aa/bb/*.val` and `*.tmp` with no lock held.
  2. In chunks of about 100 candidates: `BEGIN IMMEDIATE`; for each candidate, `SELECT 1 FROM cache WHERE filename = ?` (served by the partial index `cache_filename`); if unreferenced and still present, it is an orphan; `COMMIT`; then unlink the orphans outside the lock.

  A file listed before the lock but renamed into place by an in-flight transaction is either committed (referenced) or rolled back by the time we hold the lock. A file being unlinked by a deleter after its commit just produces an ENOENT, which both sides tolerate. Rows whose file is missing are removed with the same compare-and-delete. `check()` should not remove empty `aa/bb` directories, or a concurrent `create_dir_all` → `create_new` can race with it; if it must, the writer has to retry on ENOENT.

Caveat: SIGKILL keeps the page cache, so it never tears a file. Power loss with no fsync can leave a `.val` with garbage contents under a committed row. That's the documented no-fsync trade-off; the codec must reject a truncated value as a miss rather than return garbage.

## Experiment 3: no writes on open (D-019)

**Open sequence (check first, write only on a mismatch):**

1. `PRAGMA journal_mode` (a read). Only if it isn't `wal`: run `PRAGMA journal_mode = WAL` outside any transaction and fail loudly if the result isn't `wal`. WAL is persistent in the file, so an existing cache never needs this.
2. `PRAGMA synchronous = NORMAL` (per connection, never written to the file).
3. `PRAGMA user_version`. If it's greater than `FORMAT_VERSION`, raise `FormatMismatch`. If it's 0 (brand new), run `BEGIN IMMEDIATE`, re-read `user_version` (another process may have won), create the schema, set `user_version`, and `COMMIT`.
4. Read each setting with `SELECT value FROM settings WHERE name = ?`. Only if one differs: `BEGIN IMMEDIATE`, `INSERT OR REPLACE` the settings, `COMMIT`.

**Verification.** A cache with 1000 keys (20 of them spilled); before each case, `PRAGMA wal_checkpoint(TRUNCATE)` must return `(0, 0, 0)`. A peer connection stays open and reads `PRAGMA data_version` before and after. A child process opens the cache, does 5 `get`s, an `in`, `len`, a full key listing and `volume`, then exits. The main file's FNV-64 hash and mtime are compared with the peer still open, and again after the peer (the last connection) closes, which runs SQLite's final checkpoint.

| case | open wrote | peer `data_version` | WAL bytes after | main file (peer open) | main file (after last close) |
|---|---|---|---|---|---|
| check-first, same settings | no | 2 → 2 | 0 | unchanged | unchanged |
| check-first, same settings (again) | no | 2 → 2 | 0 | unchanged | unchanged |
| unconditional writes (control) | (yes) | 2 → 3 | 4152 | unchanged | **changed** (hash and mtime) |
| check-first, one setting changed | yes (settings only) | 2 → 3 | 4152 | unchanged | changed |
| check-first, after that change | no | 2 → 2 | 0 | unchanged | unchanged |
| check-first, no peer connection at all | no | n/a | no WAL file left | n/a | unchanged |

The control matches D-019's description of diskcache: with a peer open, a no-op rewrite leaves the main file's bytes identical and only `data_version` reveals it, until a checkpoint copies it in. So the conformance test needs both signals, and the checkpoint-before-start step, exactly as specified. Not covered here: many processes racing to create a brand-new directory with this open sequence. The design handles it (re-check `user_version` under `BEGIN IMMEDIATE`; `journal_mode = WAL` waits on the busy handler), but Phase 2's Test B has to prove it.

## Experiment 4: letting writers in during maintenance (D-010, D-011)

**Setup.** 50,000 inline rows (100-byte values), checkpointed. The `clear()` runs in the orchestrator process:

1. Take a snapshot: `snap = SELECT MAX(store_seq)`.
2. Loop: `BEGIN IMMEDIATE`; `SELECT id, filename FROM cache WHERE store_seq <= snap ORDER BY store_seq LIMIT chunk`; `DELETE` those ids; `COMMIT`; unlink their files outside the lock; optionally sleep for `pause`.

A separate writer process (opened beforehand) waits for the first chunk, sleeps a random 0-200 ms (inside the clear's duration), then does one `set` with a 1.0 s timeout. "In during clear" means the writer's commit happened before the clear finished. There were 20 trials per configuration. The writer's busy policy is either SQLite's default handler (`sqlite3_busy_timeout`; the bundled build has `HAVE_USLEEP=1`, so it backs off 1, 2, 5, 10, 15, 20, 25, 25, 25, 50, 50, 100 ms) or a custom `busy_handler` closure that sleeps `poll` and gives up after `timeout`. The closure resets its deadline when `count == 0`, the first call of each lock wait.

In every one of the more than 700 trials, `late_write_survived` was true and exactly 1 row remained. The `store_seq` snapshot bound works (D-011), and `clear()` always finished (D-010-style boundedness).

**First pass (default `wal_autocheckpoint = 1000`):** every chunk-100 configuration got in **20/20**, including SQLite's default handler with no pause (writer wait p50 0.3 ms, max 4.7 ms; clear 0.34 s). That looked too good. The cause is that after a commit pushes the WAL past 1000 pages, the committing connection runs an auto-checkpoint, and it does so *after* releasing the writer lock. That opens a multi-millisecond gap every couple of hundred chunks, and the writer slips in. Chunk 1000 was also 20/20 with a window that fit inside its 0.15 s clear.

**Second pass, with that accident removed (`wal_autocheckpoint = 0` on the clearing connection):** this is the honest case. Checkpoints can be starved by long readers or tuned differently, so fairness can't depend on them.

| chunk | pause between chunks | writer policy | in during clear | writer wait p50 / max | clear time p50 |
|---|---|---|---|---|---|
| 100 | 0 | default handler | **0/20** | 185 / 302 ms (waited out the clear) | 0.27 s |
| 100 | 0 | poll 1 ms | 1/20 | 165 / 270 ms | 0.27 s |
| 100 | 0 | poll 100 µs | 12/20 | 179 / 293 ms | 0.35 s |
| 100 | 50 µs | default handler | 9/20 | 164 / 412 ms | 0.34 s |
| 100 | 50 µs | poll 1 ms | 20/20 | 12.7 / 51 ms | 0.33 s |
| 100 | 50 µs | poll 100 µs | **20/20** | 6.3 / 8.9 ms | 0.32 s |
| 100 | 100 µs | default handler | 13/20 | 87 / 404 ms | 0.37 s |
| 100 | 100 µs | poll 1 ms | 20/20 | 12.5 / 64 ms | 0.44 s |
| 100 | 100 µs | poll 100 µs | **20/20** | 5.3 / 8.4 ms | 0.36 s |
| 100 | 250 µs | default handler | 18/20 | 16 / 482 ms | 0.54 s |
| 100 | 250 µs | poll 1 ms | 20/20 | 5.7 / 11.8 ms | 0.54 s |
| 100 | 250 µs | poll 100 µs | 20/20 | 4.9 / 8.6 ms | 0.53 s |
| 100 | 500 µs | default handler | 20/20 | 5.4 / 114 ms | 0.89 s |
| 100 | 1 ms | default handler | 20/20 | 4.6 / 6.5 ms | 1.42 s |
| 250 | 250 µs | poll 1 ms | 20/20 | 8.7 / 36 ms | 0.39 s |
| 250 | 250 µs | poll 100 µs | 20/20 | 6.8 / 11.3 ms | 0.33 s |

Total clear time with auto-checkpoint on, chunk 100: 0.34 s with no pause, 0.43 s at 100 µs, 0.56 s at 250 µs, 0.81 s at 500 µs, 1.30 s at 1 ms, 2.17 s at 2 ms. macOS `sleep` overshoots, so a "100 µs" pause costs about 150-200 µs per chunk. Chunk 50 doubles the per-chunk overhead (0.38 s with no pause, 1.37 s with a 500 µs pause); chunk 1000 takes 0.16 s.

**Reading.** SQLite's busy handler polls; it doesn't queue. With no gap between chunks, nothing reliably gets in, so the pause is required. The default handler's long backoff (up to 100 ms) samples the lock rarely and misses short gaps; it needs a pause of 500 µs or more, which nearly triples the clear time. A short custom poll catches a 50-100 µs gap every time, with single-digit-ms waits.

## Experiment 5: prefix scan

**What diskcache does.** We observed it only through a `sqlite3` trace callback (installed by wrapping `sqlite3.connect` before importing diskcache 5.6.3; no source read). Schema: `Cache(rowid INTEGER PRIMARY KEY, key BLOB, raw INTEGER, ...)` with `UNIQUE INDEX Cache_key_raw ON Cache(key, raw)`. `iterkeys` runs `SELECT key, raw FROM Cache ORDER BY key ASC, raw ASC LIMIT 1`, then pages of `SELECT key, raw FROM Cache WHERE key = ? AND raw > ? OR key > ? ORDER BY key ASC, raw ASC LIMIT 100`: 1,002 SELECTs for 100k keys, 4,002 for 400k.

**The EXPLAIN trap.** The trace callback reports the SQL with the parameters *expanded as literals*. `EXPLAIN QUERY PLAN` on that text reports `SEARCH Cache USING COVERING INDEX Cache_key_raw (key>?)`, which looks fine. With **bound parameters**, which is how diskcache actually runs it, the plan is `SCAN Cache USING COVERING INDEX Cache_key_raw`: a full index walk from the start, filtered row by row until 100 rows match. With literals the planner can fold the OR into a range; with parameters it can't. Per-page cost measured with a progress handler (`scripts/page_cost.py`, Python's sqlite 3.53.1):

| query (bound parameters) | cursor at 0% | at 50% | at 99% (400k keys) |
|---|---|---|---|
| diskcache `key = ? AND raw > ? OR key > ?` | 0.027 ms, ~900 VM ops | 8.3 ms, ~1.0M ops | **15.9 ms, ~2.0M ops** |
| row value `(key, raw) > (?, ?)` | 0.023 ms | 0.020 ms | 0.020 ms (`SEARCH ... ((key,raw)>(?,?))`) |
| plain `key > ?` | 0.019 ms | 0.018 ms | 0.018 ms |

So each page is O(position) and the whole scan is O(n²). That confirms the plan's hypothesis, and the measured totals match the roadmap's numbers: **`iterkeys()` + `startswith('d:')` takes 1.60 s at 100k keys and 25.6 s at 400k (16.0× for 4× the keys)**, with the plain full `iterkeys` at 1.65 s and 26.1 s. The fix in diskcache would be a one-line row-value comparison (worth logging in `upstream/` with this repro).

**Our keyset-paged range scan.** The same key shape as `bench/baseline.py` (`{h|d}:{i % 24}:{ip}:{i}`, 25% under `d:`). The first page is `SELECT key FROM cache WHERE key >= ?prefix AND key < ?upper ORDER BY key LIMIT ?n`, and the rest are `key > ?last AND key < ?upper`. Each page is its own statement, with no read transaction held between pages. The plan is `SEARCH cache USING COVERING INDEX sqlite_autoindex_cache_1 (key>? AND key<?)` for both. `upper` is the prefix with its last code point incremented (skipping the surrogate gap; trailing U+10FFFF dropped; an empty prefix means no upper bound). This is exact because UTF-8 byte order equals code point order under SQLite's BINARY collation. Best of 5, Rust only (no Python object creation):

| keys | page size | prefix `d:` (25%) | prefix `d:4:` (1/24) | full keyset walk + filter | diskcache iterkeys + filter |
|---|---|---|---|---|---|
| 100k | 100 | 25,000 keys in 2.3 ms | 4,167 in 0.39 ms | 9.3 ms | 1,600 ms |
| 100k | 1000 | 1.5 ms | 0.25 ms | 5.9 ms | |
| 400k | 100 | 100,000 keys in 10.2 ms | 16,667 in 1.55 ms | 38.7 ms | 25,620 ms |
| 400k | 1000 | 6.4 ms | 0.94 ms | 25.3 ms | |

Both our scans are linear (4.4× for 4× the keys). The prefix scan is about 700× faster than diskcache at 100k and 2,500× faster at 400k, before Python conversion; creating 100k `str` objects adds a few ms. Even an unfiltered `iterkeys` done with keyset paging is about 170× faster at 100k than diskcache's.

## Recommendations for Spike 4b

- **Spill protocol:** adopt it exactly as written above (temp write outside the lock; rename, upsert and commit under `BEGIN IMMEDIATE`; unlink the old file after the commit).
- **Spill read:** re-read and retry, with **8 optimistic attempts, then the locked-read fallback**. The fallback runs with the GIL released and is bounded by `timeout`. 4b should confirm that a `BEGIN IMMEDIATE … COMMIT` with no changes never bumps a peer's `data_version` (it shouldn't, since nothing is written, but it wasn't measured here), or D-019's reads-never-write test could trip under contention.
- **Opportunistic orphan delete:** `DELETE FROM cache WHERE id = ? AND store_seq = ?` with `busy_timeout(0)`, BUSY swallowed, then the connection's normal busy handler restored. It needs `&mut Connection` in rusqlite, which fits a pool checkout. Keying on `(id, store_seq)` rather than `key` is load-bearing: the key-based control destroyed thousands of live rows.
- **Spill threshold: 64 KiB.** Both 32 and 64 KiB were equally correct here; the choice isn't driven by correctness. 64 KiB halves the file count and the orphan exposure for mid-size values, and keeps more values on the µs inline path (no `py.detach`, no extra syscalls; a spilled set costs about 0.23-0.26 ms). Inline-vs-spill read latency between 32 and 128 KiB wasn't measured; if 4b wants a sharper number, that's a 10-minute benchmark with this harness. The conformance adapter's `spill_threshold` must equal whatever is chosen (Test A's `spill_min` is then 128 KiB).
- **File naming:** `aa/bb/<32 random hex>.tmp` → `aa/bb/<same>.val`, relative path stored in `filename`. A partial index `ON cache(filename) WHERE filename IS NOT NULL` serves `check()`. Never reuse names.
- **`check()` orphan pass:** as designed above. It's exact for `.val` with no grace period, because renames only happen under the write lock; `.tmp` files are cleaned up only once older than a grace period.
- **Maintenance chunking:** chunk **100 rows** (250 is also fine: 0.33-0.39 s vs 0.32-0.36 s, with slightly longer writer waits). Bound by a `store_seq` snapshot, ordered by `store_seq` on its index, with spill-file unlinks after each chunk's commit, outside the lock.
- **Pause between chunks: 100 µs** (a sleep with the GIL released) after every chunk's commit. In the adversarial no-checkpoint case it cost about +30% clear time (0.27 s → 0.36 s for 50k rows) and turned 0/20 into 20/20. Don't rely on auto-checkpoint gaps.
- **Busy handler:** a custom `busy_handler` closure on every pooled connection, replacing `sqlite3_busy_timeout`. Poll every **100 µs** (1 ms also reached 20/20 with a 50 µs+ pause, but with 2-6× longer waits), deadline = the cache's `timeout`, reset at `count == 0`, and raise `Timeout` when it returns false. To cap CPU for long waits, a hybrid (100 µs for the first ~20 ms, then 1 ms) is a reasonable refinement; that hybrid wasn't measured. The handler runs inside SQLite while a pooled connection is held and must never call Python, so the native clock must be used, never `clock=`.
- **Open:** the check-first sequence above, verified to write nothing when settings match. `FormatMismatch` when `user_version` is newer than ours. No `PRAGMA optimize` on close.
- **Prefix-scan API: `iterkeys(*, prefix: str | None = None, reverse: bool = False)`**, a keyword-only `prefix=` on the existing method, rather than a new `keys_with_prefix()`:
  - It keeps the drop-in signature (diskcache code keeps working) and is where users already look; the Talk Python call site becomes `cache.iterkeys(prefix='d:')`.
  - It doesn't add a second iterator with subtly different semantics (consistency, ordering, chunking) to keep in sync.
  - `NullCache.iterkeys` and the `CacheLike` Protocol gain the same keyword (the signature-equality test enforces it). With `invalidate=`, it forwards to the real cache.
  - Semantics: keys in key (BINARY/UTF-8) order, like diskcache's `iterkeys`; fetched in Rust in chunks of about 256-1000 (page 1000 was 1.6× faster than 100), with the connection returned to the pool between chunks (D-017); not a snapshot (concurrent writes may or may not be seen, as with diskcache).
  - `reverse=True` uses `key < ?cursor AND key >= ?prefix ORDER BY key DESC`.
  - Expired-but-unswept rows are included, as the NullCache rule says `iterkeys` behaves.
  - When non-`str` keys arrive (Phase 6a), `prefix=` applies only to `str` keys. The typed key column then needs its index to lead with the key type, `(key_type, key)`, so the range stays a SEARCH.
  - A natural follow-up is `delete_prefix(prefix) -> int` (chunked like `clear()`), since prefix invalidation is the common reason to scan. It's optional and not needed for Phase 2.

## Limits of this spike

macOS on APFS only; Linux wasn't run, though the protocol relies only on POSIX rename/unlink semantics. One connection per process; no pool, threads or GIL were involved. The 4 or 6 reader processes stand in for threads. SIGKILL is not power loss. Experiment 4's writer is a single `set`; many concurrent writers during a `clear()` weren't measured. The diskcache numbers were taken in a venv with Python's sqlite 3.53.1, not the system Python a Talk Python deploy would use.
