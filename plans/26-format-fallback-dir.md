# Plan 26: A format mismatch opens a versioned subdirectory instead of failing

Written 2026-11-13. Today a cache whose database (or FanoutCache manifest) is in another format raises `FormatMismatchError` at open ([open.rs](../crates/diskcache-oxide-core/src/open.rs) `prepare_creating`, [fanout.rs](../crates/diskcache-oxide-core/src/fanout.rs) `Manifest::parse`). That's the safest behavior, and it's turned out to be operationally painful. An upgrade becomes stop, delete, start across every process. A straggler on the old release can re-create the old format between the delete and the new start, and then the new release fails again. The [upgrading recipe](../docs-source/recipes/upgrading.qmd) already tells users to put each format series in its own directory by hand. This plan makes the library do that for them.

Reviewed by the architect on 2026-11-13; its findings (H1-H3, M1-M7 and the lows) are folded in below and marked where they changed the plan.

## Decisions (Michael, 2026-11-13)

1. **Redirect, don't refuse.** When opening `D` would fail only because the cache there is in another format, the cache opens `D/diskcache-oxide-format-N/` instead, where N is this build's format, and logs a warning. The redirect applies only when not taking it would fail.
2. **Both directions.** A newer build meeting an older cache redirects, and so does an older build (one that has this feature) meeting a newer cache, for example during a rollback. Only builds with this feature redirect: 0.1.2 and 0.2.3 still raise when they meet a newer cache.
3. **Name:** `diskcache-oxide-format-N`, matching `diskcache-oxide.sqlite` and `diskcache-oxide-fanout`. N is the database `FORMAT_VERSION`, not the library version.
4. **`cache.directory` reports the resolved path** (`D/diskcache-oxide-format-N`), where the files actually are, so code that inspects or removes `cache.directory` acts on the real cache.
5. **No opt-out knob.** Anyone who wants strictness can alert on the warning.
6. **The warning is logged once per process per resolved path**, on the `diskcache_oxide` logger at `WARNING`. Every worker restart repeats it until the old files are gone.
7. **A deploy never starts cold unless a format mismatch forces it.** The upgrading recipe's `format_series()` pattern (a directory per minor version) goes, since it throws away a warm cache on every minor bump. One paragraph tells existing adopters it still works and how to go back to a single directory.
8. **The redirect race is documented, not locked against** (see below).
9. **A cleanup helper, `remove_old_formats()`, plus docs** (section below): a method on the cache handles. It removes older formats only, deletes only names diskcache-oxide writes, and doesn't check whether another process still uses them. Calling it means "remove them now". The caller decides when no old worker is left.
10. **Version:** the next release is **0.3.5**. 0.2.4 in `Cargo.toml` is already spent even though it never shipped; the minor bump is for the behavior change.

## The resolution rule

N is `FORMAT_VERSION` (3 today), and `S = D.join("diskcache-oxide-format-N")`. It's always built by joining onto `D` as given, never from a canonical path, so `\\?\` never reaches `directory` on Windows (plan 25, R-L13). For `Cache(D)`:

0. **Guards, before anything else (architect H1, M2):**
   - If `D`'s own name matches `diskcache-oxide-format-<digits>`, `D` is opened as-is with no redirect. A `Cache(cache.directory)` built by user code must not nest a second level.
   - If `D.parent()/diskcache-oxide-fanout` exists, raise `RoleMismatch`, exactly as for a current-format shard. Today `prepare_creating` checks `user_version` before the role stamp, so `Cache(D/000)` on an old FanoutCache's shard would otherwise see `FormatMismatch` and redirect into the old root's shard directory. This is a stat only, so it works for any format. `test_caches.py:663` already expects `LayoutMismatchError` there.
1. **`S` exists:** use `S`, even if the root is now empty or in format N. The choice is sticky. Without that, worker A sees a v2 root and goes to `S`, ops deletes the old root files, and worker B finds an empty root and creates a second format-N cache there. Invalidation, locks, queues and run-once claims would split silently.
2. **Root is empty or in format N:** use the root, as today. After an open at the root that *created* the database, re-stat `S`. If it appeared meanwhile, release the root store and move to `S`, as the double `refuse_manifest` check does in `Store::open_until` (architect, low). This shrinks the race below to the stat-to-create window.
3. **Root is in another format:** create `S` (non-recursive, 0700, like `ensure_shard_directory`), open it, and log the warning. If the `mkdir` fails with `NotFound` (`D` was deleted mid-open), rerun the rule once under the same deadline. Never recreate `D` from inside `S`'s open: `ensure_directory(S)` would recreate `D` with umask permissions.

Inside `S`, nothing redirects again. Only the attempt at the root may redirect: that's decided by structure, not by comparing the error's path with `conn.path()`, whose spelling differs for relative paths and symlinks (architect M2). A database in `S` that isn't format N is a genuine `FormatMismatchError`, with its own message.

**Check order in `prepare_creating` (architect M2).** Read `user_version` first, refuse a nonzero non-N value, then run `ensure_wal`. Today `ensure_wal` runs first, so an older build meeting a newer cache that isn't in WAL mode would run `PRAGMA journal_mode = WAL` on it, or time out on its lock instead of redirecting. A new database is still created in WAL mode, because creation comes after.

**What doesn't redirect:** `LayoutMismatchError` and everything under it, meaning `RoleMismatch` (a `Cache` on a FanoutCache root or shard, old or current format, or a `FanoutCache` on a plain cache) and `ShardCountMismatch`. These are config bugs, and forking the cache would hide them. `ManifestInvalid` (a damaged or foreign manifest) doesn't redirect either. Neither does any I/O, timeout or SQLite error.

**Known race, documented and not locked against:** if the old root is deleted in the window between A's stat of `S` and A's create at the root, or between A's format check and A's `mkdir` of `S`, a concurrent B can end up at the root while A is in `S`. The result is a split cache, not corruption. Rule 2's re-stat closes most of it. Handles that land at the root stay there for the life of their `Store`: a closed handle that's reused rejoins by `Store.directory` and never re-resolves, and only a new constructor does (architect, low). Closing the race completely would need a lock file in `D`, which old builds wouldn't honor anyway.

**0.2.3 doesn't know the sticky rule (architect M5; Michael: document it, safe but not ideal).** Once the root's old cache is removed, a 0.2.3 worker finds an empty root and creates format 3 there, while 0.3.5 and later stay in `S`. That's a same-format split, and `remove_old_formats()` never removes it because it's the current format. It is **safe**: both are valid format-3 caches, each worker gets a working cache, and nothing is corrupted or lost. It's **not ideal**: the 0.2.3 workers and the newer ones each warm their own cache, and invalidation and coordination (locks, run-once claims, rate limits, queues) don't cross between them, just as during any overlap. Upgrading the 0.2.3 workers, or waiting to clean up until they're gone, avoids it. That's recommended, not required. Once the last 0.2.3 worker is gone, the root's format-3 cache is simply unused: deleting it by hand is optional, and `remove_old_formats()` deliberately leaves it.

## FanoutCache

The whole root redirects together, never one shard. `FanoutCache(D)`:

1. The same guards, then: `S` exists, so use `S` as the root.
2. Otherwise, `open_manifest(D)`. If the manifest's own `format` line is another version (`Error::FormatMismatch` from `Manifest::parse`), redirect.
3. Otherwise, open the shards. 0.1.2 to 0.2.3 kept manifest format 1 and changed only the shard databases, so the shard databases decide too. **Order (architect M4):** `open_shards` today opens 0..n in order and creates each as it goes (`store/shards.rs:56-71`). A missing or 0-byte shard before the first old one would be created in format 3 inside the old root, and releasing the store doesn't undo that. So the redirect-capable path first opens the shards whose database file already exists, and creates the missing ones only after all the existing ones pass. If any existing shard gives `FormatMismatch`, release the shards already opened and redirect the whole root to `S`.
4. In `S`, a normal `open_manifest` plus shard opens with no further redirect.

**Mixed roots.** A root with some old and some new shards doesn't only come from deleting single shard directories (architect M4). An old and a new FanoutCache racing on a fresh root both write manifest format 1, and each shard's creation is won separately. The redirect handles it, since nothing in such a root can be served whole. The old build loses, as in the plain case. Document the creation race beside the plain one.

**`Caches` with a stored shard count (architect, low).** `ForNewRoot` can raise `StoredShardsRefuseSettings` against the old root's count (`cache/fanout.rs:1309-1318`) before any shard's format is seen. Check shard 0's format before that comparison, so an old root redirects instead of failing on settings that belong to a cache it won't use.

**Frozen contract (architect, low).** These must never change, or "both directions" breaks for every later build: `DB_FILENAME`, `user_version` as the format marker, the manifest's first two lines, the name `diskcache-oxide-format-N`, and rule 1. A change to the manifest format also bumps `FORMAT_VERSION`, so `S` always names a format both parts agree on. All of this goes in a comment beside `MANIFEST_FORMAT` and in `schema.rs`'s module doc.

## Cleanup: `remove_old_formats()`

`cache.remove_old_formats()` on `Cache` and `FanoutCache` returns `[]` on `NullCache`. `caches.remove_old_formats()` on the `Caches` registry runs it for every registered cache that's open and returns the combined list. Each returns the paths it removed, as `list[str]`, and logs at `INFO` what it removed and at `WARNING` anything it left and why.

**What it considers.** The handle knows its root `D` (as passed) and its resolved directory. Candidates are decided by **content, never by name** (architect H2):
- The root's own cache: a plain database with `0 < user_version < N`, or a FanoutCache root whose manifest format is older, or whose shard 0 database is `0 < v < N`.
- Each `D/diskcache-oxide-format-M` (a real directory, not a symlink) whose database, or manifest, or shard 0 database reads as `0 < v < N`. A format-N cache that someone opened at a path named `diskcache-oxide-format-2` is kept.
- Before unlinking any database, look its file id up in this process's store registry, the same way `Store::lookup` does. If a live or retired `Store` holds it, leave it and warn.

A newer format is never removed, at the root or in a subdirectory, so a rolled-back build can't wipe the newer release's warm cache. The current format is never removed either, including a format-N database at the root left by the race or by a 0.2.3 worker. A `user_version` of 0 (a database mid-creation, possibly a fresh old-format root right after a removal) or any read error means "unknown": leave it and log.

**Reading a candidate's format (architect M6).** `open::connect` plus `PRAGMA user_version` only: no `ensure_wal`, no pin, and under a gate level as `open_store` does, since connections close under the gate. It's a function that returns the number, so the `Connection` is dropped before any unlink, which also frees the handle on Windows. It never reads the header bytes directly: page 1 can still be in the WAL, and closing a plain `File` drops every POSIX lock SQLite holds on that file in this process (the hazard `open_pin` documents).

**Only names diskcache-oxide writes, and never follow links (architect M7).**
- Plain cache: `diskcache-oxide.sqlite`, `-wal`, `-shm` and `-journal`, and inside each two-character lowercase hex directory only names that pass the `value_path` rule (`aa/<32 hex starting with aa>.val` and `.tmp`).
- FanoutCache root: the `diskcache-oxide-fanout` manifest, its `diskcache-oxide-fanout.tmp-*` files, and the `000`..`NNN` shard directories, each holding a plain cache's files.
- `symlink_metadata` throughout. A symlink is never followed, whether it's a hex directory, a shard or a `diskcache-oxide-format-M`.
- Anything else is left in place and named in the log. A directory is removed with `rmdir` only once it's empty, never recursively. Format 1 (the yanked 0.1.0.dev1) used two-level `aa/bb` value directories. A strict filter won't fully remove those, so what's left is logged.

**Order (architect M1, M7).**
- Value files go first.
- Then the database files, unlinked **back to back with nothing in between**: database, `-wal`, `-shm`, `-journal`. SQLite binds the database inode at open but opens `-wal` and `-shm` by path later, so a gap lets an old process that opens by path pair a fresh database with the old live WAL, or the old database with a new WAL.
- A FanoutCache's shards go in **descending order, shard 0 last, then the manifest**. A cleanup interrupted halfway (a crash, or a shard held open on Windows) then keeps the shard 0 and manifest that detection relies on, and the next call recognizes the root and finishes.

**No in-use check (Michael's decision).** Calling it removes the old cache. The caller decides when no old worker is left, as with deleting the directory by hand today. What a still-running old worker sees, stated plainly in the recipe and the docstring:
- **Unix:** removal while an old worker runs can corrupt that worker's cache until it restarts, just as `rm -rf` by hand can (architect M1): "malformed" errors or wrong results from the old cache, not just misses. A reopen can create a fresh, empty old-format database at the root, which the sticky rule keeps new workers away from and the next call removes. It stays confined to the old-format files: new builds never open the root once `S` exists. Nothing the old worker held survives: queued items, counters and sessions in the old cache are gone.
- **Windows:** an open file can't be deleted. Whatever is still open is left in place and named in a `WARNING`, and the call carries on with the rest. It never raises for that. Because value files go first, an old worker that holds its database open has already lost its spilled values, which read as misses (D-013). A later call, once the old worker is gone, finishes the job. 0.1.2 and 0.2.3 have no Windows wheels, so this path matters only for future formats.
- Any other I/O error on a file or directory it means to remove is logged and left, and the call goes on. It raises only if `D` itself can't be read.

**Sticky rule interaction.** Removing the root's old cache leaves `D` holding only `diskcache-oxide-format-N/` (plus anything foreign), and rule 1 keeps every opener there. The cache never moves back to the root on its own. That's intended: moving it would be a cold start.

## Where it lives (architect's answers)

**Core: a new `layout.rs`.** `resolve<T>(root, attempt: impl FnMut(&Path) -> Result<T>) -> Result<Resolved<T>>` holds rule 0 (both guards), rule 1, the `mkdir` of `S`, the `NotFound` retry and rule 2's re-stat.
- Plain `Cache`: wraps `Store::open_until`.
- FanoutCache: the py crate's open body becomes the closure, with the shard count captured in a `Cell` for `open_error`.
- Not inside `Store::open_until`: shards call that (`shards.rs:65`) and must never redirect.

**Catch-and-retry, confirmed clean.** The `FormatMismatch` returns from inside `lockwait::retry`'s closure (`store.rs:473-490`) before the registry, `claim_live`, `open_pin` and `stats::for_file`. `retry` retries only `Timeout`, and the connection drops before the gate guard. So a refused root open leaves no store, pin, pool entry or stats entry. The one side effect is SQLite's own: if this was the last connection on the old database, closing it checkpoints the old WAL. Test 1 therefore asserts unchanged content and `data_version`, plus an unchanged file hash when the WAL was already empty, as D-019's tests do.

**Shape (architect M3).** `Resolved<T> { value, directory, redirect: Option<Redirect> }` comes back beside the store, not through it: a `Store` is shared per file and a FanoutCache has N of them, while a redirect is a fact about one open.

```rust
pub struct Redirect { pub from: PathBuf, pub found: Found, pub expected: i64 }
pub enum Found {
    Database { path: PathBuf, version: i64 },   // the root database's user_version
    Manifest { path: PathBuf, format: i64 },    // the manifest's format line (a different number space)
    StillPresent { path: PathBuf },             // rule 1 taken and a stat shows old root files remain
}
```

Older or newer is derived from `found` against `expected`, per variant. `StillPresent` is what makes decision 6 hold: on the sticky path the root isn't opened, so there's no format to name, and the wording says "old files remain at D" without one.

**Nothing in core reopens by the requested path.** Rejoin and `open_pin` use `self.directory`, stats are keyed by file id and canonical path, fork resets each `Store` in place, and plan 18 is unaffected. Only the py crate holds the requested path.

**Py crate.**
- `HandleConfig::new` (`cache.rs:155`) gets the resolved path, so `directory`, `repr`, durable failure reports (`cache.rs:682`) and `on_decode_error` messages (`cache.rs:1620-1627`) all name `S`.
- **Pickling keeps the requested `D`** (`cache.rs:137, 1787-1790`), and the child re-resolves. A child that unpickles after cleanup or a format change then lands where a fresh open would. Tested with a round trip.
- **The warning (architect H3)** dedupes through a module-level Python dict keyed by `os.path.realpath(S)`, using `dict.setdefault`, the same pattern as `caches.py`'s `_REPORTED_SHARD_MISMATCHES`. No `Mutex`: on 3.14t a thread holding it at `fork()` would hang the child's first warning. The message names the old database, its format (when known), this build's format, the path now in use, and what to do:

```text
diskcache-oxide: /app/caches/SOME_CACHE holds a format-2 cache (from an older diskcache-oxide), so this build (format 3) is using /app/caches/SOME_CACHE/diskcache-oxide-format-3 instead. Once nothing older uses it, call remove_old_formats() or delete the old files in /app/caches/SOME_CACHE, keeping the diskcache-oxide-format-3 directory.
```

The newer-format wording says "from a newer diskcache-oxide" and suggests upgrading this process. The `StillPresent` wording names no format.

**`Caches` registry.** A format mismatch at a registered cache's root now opens normally (redirected) instead of handing out a `NullCache` and retrying. `on_open_error` and the retry stay for everything else. `caches.py` updates its comment at line 76, its module docstring (lines 21-23), and `directory(name)`'s docstring ("where the named cache lives" is now the resolved path).

**`FormatMismatchError` stays.** It's still raised for a mismatch inside `S`, an unreadable manifest format in `S`, and anything a redirect can't fix. The message no longer says "delete the cache's directory" in the root case, which doesn't raise any more, and a mismatch inside `S` gets its own wording (`error.rs:165-174`).

## Old builds next to the subdirectory

The architect verified this against both tags, in Rust and Python:
- **Sweep:** `spill::sweep` filters to two-character hex names (v0.1.2 `spill.rs:216-239`; v0.2.3 `226-249`), and `remove_dead_temp_files` matches only `<32 hex>.tmp`.
- **Unlinks:** they go through `value_path`'s name filter.
- **`check()`:** 0.2.3's `check_files` skips non-hex directories, and 0.1.2 has no file walk.
- **`remove_dir_all` and Python:** `remove_dir_all` appears only in tests and `testutil`, and neither tag's Python package touches files.

So an old build sharing `D` never touches `S`. A FanoutCache root's `000`..`NNN`, manifest and temp files don't collide with `diskcache-oxide-format-N`, and nothing in either tag counts the root's entries. Format 1 (0.1.0.dev1, untagged and yanked) wasn't verified.

## Tests

**Core unit tests (`layout.rs`, `open.rs`, fanout):**
1. v2 root, no `S`: opens `S`. The root's content and `data_version` are unchanged, and so is its file hash when the WAL was empty. `redirect` is `Found::Database { version: 2 }`.
2. Root in format N+1 (simulated): opens `S` (backward direction). A newer root that isn't in WAL mode is not converted, and nothing times out on its lock.
3. `S` exists and the root is empty: uses `S` and creates nothing at the root. With old root files present, `Found::StillPresent`; with none, no redirect at all.
4. `S` exists and the root is format N: uses `S`.
5. Empty `D`: uses the root, with no `S` and no redirect. If `S` appears between the stat and the creating open, the open moves to `S` (rule 2's re-stat, using a test hook).
6. `S` holds a non-N database: `FormatMismatch` naming `S`'s database, with the inside-`S` message and no second-level subdirectory. `Cache(S)` opened directly never redirects.
7. `RoleMismatch`, `ShardCountMismatch` and `ManifestInvalid` at the root still raise and create nothing. **`Cache(D/000)` on an old-format and on a current-format shard both raise `RoleMismatch`** (H1).
8. A refused root open leaves no store, pin, pool entry or stats entry (registry count, stats registry and open fds unchanged).
9. A redirected open with an explicit `size_limit` writes nothing at the root, like `open.rs:489-505`.
10. The `mkdir` of `S` gets `NotFound` (`D` deleted mid-open): rule 1 reruns once, and `D` is never recreated with umask permissions.
11. Two threads and two processes racing the first redirect agree on `S`: one creates it, and neither errors.
12. FanoutCache:
    - A manifest format mismatch redirects the root.
    - Manifest current but shard databases old: the whole root redirects, and the shards opened before the failure are released.
    - An old root with a missing or 0-byte shard before the first old one creates nothing in the old root (M4).
    - `S` exists means `S` is used.
    - `ForNewRoot` against an old root with another stored count redirects instead of raising `StoredShardsRefuseSettings`.

**Python unit tests (`tests/unit/test_format_fallback.py`):**
- `Cache.directory`, `FanoutCache.directory`, `repr` and a durable failure report all name `S` after a redirect.
- The warning is logged once per process per path. A second handle on the same `D` is quiet, and another `D` warns again. On 3.14t, many threads opening at once plus a `fork()` mid-flight never hang the child, and the child stays quiet.
- A pickle round trip of a redirected cache lands in `S` again.
- `Caches.register` on an old-format directory returns a working cache, not a `NullCache`, and `on_open_error` isn't called. Rewrite `test_caches.py:299-313` (future format gives a `NullCache`) to match.
- The redirected cache works across `fork()`, like any cache.
- Update the "delete" message assertion at `open.rs:519-529`.

**`remove_old_formats()` tests:**
- An old plain root: its database, WAL, SHM and value files are removed. `S` and a foreign file in `D` are kept, and the foreign file is named in the log.
- A directory named `diskcache-oxide-format-2` holding a current-format database is kept, and so is a database a `Store` in this process holds (H2).
- An older `diskcache-oxide-format-M` (by content) is removed. A newer one and a format-N root are kept, and a `user_version` of 0 is left and logged.
- Symlinks are never followed: a symlinked hex directory, shard or format directory pointing elsewhere is left alone, and so is its target.
- An old root held open by another process (a subprocess with a raw `sqlite3` connection) is removed anyway on Unix. On Windows the open files are left and named in a warning, the call doesn't raise, and the value files are already gone.
- A FanoutCache root in an older format: manifest, temp files and shard directories are removed, in descending order with shard 0 last. When a shard is held (Windows) or the removal is interrupted, the next call still recognizes the root and finishes. An unknown entry inside a shard directory keeps that directory (`rmdir` fails and it's logged).
- `NullCache.remove_old_formats()` returns `[]`, and `Caches.remove_old_formats()` covers every open registered cache.
- Calling it twice is harmless, and with nothing old present it returns `[]` and logs nothing at `WARNING`.

**Mixed-version test (`tests/unit/test_mixed_versions.py`, needs `DCO_OLD_PYTHON` with 0.1.2; Unix only):**
- Keep "0.1.2 refuses a format-3 cache" and "a handle open across a recreate".
- Replace "this build refuses 0.1.2's cache": 0.1.2 creates `D` with data. This build opens `D`, lands in `S` and writes there. 0.1.2 reopens `D`, reads its own data, and runs `set`, `delete`, `cull`, `clear` and `close`. `S` is untouched, hashed before and after. `fingerprint`'s `rglob` (lines 98-104) excludes `S` when asserting the root is unchanged.
- `remove_old_formats()` removes the 0.1.2 cache. A 0.1.2 handle open across the removal never touches `S`, and a 0.1.2 reopen creates an old-format root that the next call removes again.
- The docstring is rewritten. 0.2.3 is format 3, so it lands at the root only if the root is empty. The M5 split is documented there rather than tested.

**Conformance:** no change. The redirect and the helper are oxide-only.

## Docs (Michael asked that this be explicit)

Every page below currently tells users that a format change means stop, delete, start, or describes the refusal. Each one changes to match the code and the tests:

- **[recipes/upgrading.qmd](../docs-source/recipes/upgrading.qmd): rewrite.**
  - The new story: install the new release and restart. A release that keeps the format keeps the warm cache. One that changes it puts the new cache in `diskcache-oxide-format-N` beside the old one, old and new workers run side by side, and the warning says so.
  - Clean up by calling `remove_old_formats()` (or `caches.remove_old_formats()`) once no old worker is left: from the deploy script after the old workers stop, from a job [one worker claims](../docs-source/recipes/run-once.qmd), or at startup if the deploy never overlaps old and new.
  - **A `{.callout-warning}` (Michael asked for this to be clear), next to the first `remove_old_formats()` example and not buried later:** it doesn't check whether an old worker still uses the files. Called while one runs, on Linux and macOS it can corrupt that worker's cache until the worker restarts (errors such as "database disk image is malformed", or wrong results, not just misses), just as `rm -rf` on the directory would. Everything that worker kept there (queued items, counters, sessions) is lost. The damage stays inside the old-format files: new workers never read them. On Windows, files an old worker holds open are left in place with a warning, and the next call finishes the job. So: stop or replace the old workers first, drain any queue, then call it.
  - Show what it removes, what it never removes (newer formats, the current format, anything it didn't write, symlink targets), and the manual equivalent.
  - **A `{.callout-note}` on 0.2.3 workers (M5), phrased as safe but not ideal, with no "must":** 0.2.3 doesn't know about `diskcache-oxide-format-N`. After a cleanup, a 0.2.3 worker creates its own format-3 cache at the root while newer workers keep using the subdirectory. That's safe: both caches work, and nothing is corrupted or lost. But the two sets of workers no longer share a cache, so invalidation, locks, run-once claims and rate limits don't cross between them. If that matters to your app, upgrade the 0.2.3 workers, or clean up after they're gone. Otherwise it's fine to leave, and once the last 0.2.3 worker is gone, the root's cache is just unused. Delete it by hand if you like; `remove_old_formats()` leaves it, because it's the current format.
  - Keep "While both versions run" (split invalidation and coordination) and "State that isn't really cache".
  - Drop the `format_series()` pattern, with one paragraph for existing adopters.
  - Add both races and the "0.2.3 and earlier still refuse a newer cache" caveat, and update the page's `description`.
- **[user_guide/errors.qmd](../docs-source/user_guide/errors.qmd):** the `FormatMismatchError` table row and section now cover only what's left: a mismatch inside the versioned subdirectory, or an old build (0.2.3 or earlier) meeting a newer cache. The redirect warning, both wordings, gets a short section.
- **[user_guide/caches.qmd](../docs-source/user_guide/caches.qmd):** a format mismatch no longer produces a `NullCache` and retry loop. Add `remove_old_formats()` on the registry.
- **[user_guide/fanoutcache.qmd](../docs-source/user_guide/fanoutcache.qmd):** the whole root redirects, never one shard, and the creation race.
- **[user_guide/storage-and-wal.qmd](../docs-source/user_guide/storage-and-wal.qmd):** what can appear in a cache directory now includes `diskcache-oxide-format-N/`.
- **[user_guide/null-cache.qmd](../docs-source/user_guide/null-cache.qmd):** `remove_old_formats()` returns `[]` there.
- **[user_guide/switching-from-diskcache.qmd](../docs-source/user_guide/switching-from-diskcache.qmd), [user_guide/roadmap.qmd](../docs-source/user_guide/roadmap.qmd), [index.qmd](../docs-source/index.qmd), [recipes/index.qmd](../docs-source/recipes/index.qmd), [recipes/monitoring.qmd](../docs-source/recipes/monitoring.qmd), [README.md](../README.md):** each mentions format changes or upgrades. Bring each mention in line, and in monitoring add the redirect warning as something worth alerting on.
- **[skills/diskcache-oxide/SKILL.md](../docs-source/skills/diskcache-oxide/SKILL.md):** the agent skill's upgrade and error guidance.
- **[python/diskcache_oxide/_core.pyi](../python/diskcache_oxide/_core.pyi):** the `Cache` and `FanoutCache` docstrings (`directory`, the `Raises:` sections, `FormatMismatchError`), plus the new `remove_old_formats()`. That docstring opens with the same live-old-worker warning as the recipe's callout, in plain words (no in-use check; can corrupt a running old worker's cache on Unix; leaves held files on Windows), so it shows up in IDE hovers and the API reference too. `tests/unit/test_stubs.py` keeps these honest. Add `remove_old_formats` to `NullCache`, `Caches` and the `CacheLike` protocol in `protocol.py` if it belongs there.
- **Runtime docstrings and module docs:**
  - `crates/diskcache-oxide-py/src/lib.rs:54-56`, the runtime `FormatMismatchError` docstring.
  - `python/diskcache_oxide/__init__.py:21`.
  - `crates/diskcache-oxide-core/src/schema.rs`'s module doc, which also takes the frozen contract.
  - `error.rs:165-174`, the remedy text.
- **[tests/DIVERGENCES.md](../tests/DIVERGENCES.md) and [user_guide/divergences.qmd](../docs-source/user_guide/divergences.qmd):**
  - A new **D-059** (kind: behavior): diskcache's `directory` is the path given, while oxide's can be `D/diskcache-oxide-format-N` after a redirect. It also covers the redirect itself, since a diskcache user wouldn't expect a cache to move into a subdirectory. The new tests reference it with `backend_only`/`covers` as appropriate.
  - Add `S` to **D-015**, as a new 0700 directory oxide creates.
  - Add to **D-019**: a refused root open writes nothing, and `remove_old_formats()` is the one call that deletes database files.
- **[CHANGELOG.md](../CHANGELOG.md):** the preamble ("caches made by an older format must be deleted when you upgrade") changes. Under Unreleased, Changed: a format mismatch now opens `diskcache-oxide-format-N` and logs a warning instead of raising, and `directory` reports the resolved path. Under Added: `remove_old_formats()`.
- Then `make docs`, and commit the regenerated `site/`.

## Release

The default changes from raising to redirecting and there's new API, so this is a minor bump. The next release is **0.3.5**: `Cargo.toml`'s 0.2.4 (the unreleased Windows work) counts as spent, and the version in `Cargo.toml` becomes 0.3.5 when this lands. It doesn't change the on-disk format: format stays 3. It helps anyone still on 0.1.2 right away: 0.3.5 meeting their format-2 cache goes to `diskcache-oxide-format-3`, and `remove_old_formats()` clears the old one once their 0.1.2 workers are gone.

## Steps

1. `prepare_creating`'s check order (`user_version` before `ensure_wal`), with test 2. Clippy and `cargo test` must pass.
2. `layout.rs` and plain `Cache` resolution: guards, rules 1-3, the re-stat, the `NotFound` retry and `Redirect`/`Found`, with core tests 1 and 3-11.
3. FanoutCache resolution: existing shards first, rollback, the `ForNewRoot` ordering, with test 12.
4. Py crate: resolved `directory` in `HandleConfig`, pickling of `D`, the warning dict, `Caches`. Python unit tests.
5. `remove_old_formats()`: format reading, the registry check, ordered removal in core, the methods in the py crate, `NullCache` and `Caches`, and its tests.
6. Mixed-version test against the 0.1.2 wheel (`scripts/mixed-version.sh`).
7. Docs, DIVERGENCES, CHANGELOG, `.pyi`, runtime docstrings, skill, the version bump to 0.3.5, then `make docs`.
8. `scripts/check.sh`, then the architect's review of the finished change.

## Done when

`cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test` and `RUSTDOCFLAGS="-D warnings" cargo doc --workspace --no-deps` all pass. So do the unit and oxide conformance suites on 3.14 and 3.14t, the mixed-version test with `DCO_OLD_PYTHON`, ruff and ty. `make docs` has run with `site/` committed, and `scripts/check.sh` passes. The architect has reviewed the plan (done 2026-11-13) and reviews the finished change.

## Log

- 2026-11-13: plan written; interview with Michael (decisions 1-10); architect review folded in (H1 shard guard, H2 content-based cleanup and registry check, H3 lock-free warning dedupe, M1 back-to-back unlinks and honest docs, M2 structural redirect, check order and no nesting, M3 `Redirect`/`Found`, M4 existing shards first, M5 the 0.2.3 split, M6 format reading, M7 removal order and the name filter, plus the lows).
- 2026-11-13: Michael: the docs must state clearly, in a warning callout and the docstring, what `remove_old_formats()` does to a still-running old worker. The 0.2.3 split is documented as safe but not ideal, with upgrading those workers recommended, never required. Implementation waits until the docs reorganization (`docs-source/`) lands, so it starts from a clean branch.
- 2026-11-13: built on branch `plan-26-format-fallback` (steps 1-7). `layout.rs` (resolution), `cleanup.rs` (`remove_old_formats()`), `open::connect_existing`, `Store::open_resolving` and `Store::is_open_in_process`, existing shards opened before missing ones, the py crate's redirect warning and methods, `Caches.remove_old_formats()`, tests (core, unit, conformance D-059, mixed-version against the 0.1.2 wheel) and the docs. Version 0.3.5. `scripts/check.sh` passed on all four Pythons before the review below.
- 2026-11-13: architect review of the finished change, folded in: (high 1) an empty database file is removed only at the root and only once it's `EMPTY_GRACE` (10 minutes) old, and a shard whose database reads as this build's format or newer is never removed; (M2) format reads use `open::connect_existing`, which never creates the file; (M3, M4) stale and overstated docs corrected: only the old cache's format is read, and SQLite may checkpoint its WAL when that read closes the last connection; (M5) a mismatch inside a versioned subdirectory says to open its parent, and the docs say to reopen with the original directory; (lows) the root handle is released on a failed stat, `is_format_dir` checks the absolute path, a versioned subdirectory never creates its parent (`ensure_directory`), `read_format` skips a database this process has open, retired stores count as open, the shard count of a redirected root isn't quoted in later errors, and the value-file name rule lives in `spill::is_value_file_name`.
- Deviations from the plan, all deliberate: the sticky path reads the root's format (one connection, only while root files exist) rather than a stat, so it names the format and stays quiet about a current-format root; `Caches.remove_old_formats()` returns a dict per name, like `expire_all`; foreign entries at the root aren't logged (the subdirectory and the app's own files would be noise every call), only those inside a cache being removed; an empty database file found during a mixed-version run (a 0.1.2 handle reusing a removed path) led to the zero-length rule above.
- Open: review low 11, whether `FanoutCache(D, shards=n)` on an old root with another count should redirect (check shard 0's format before `ShardCountMismatch`) as the registry's `ForNewRoot` open now does. Michael's call.
- 2026-11-13: Michael: no soak for this change. It changes where a cache is opened at startup, not the engine's running behavior, so it works or it doesn't, and the unit, conformance and mixed-version tests decide. Great Docs moves to `>=0.17.0` (pyproject and uv.lock); `site/` rebuilt with it. Venvs are refreshed with `uv pip install`, never `uv sync`.
