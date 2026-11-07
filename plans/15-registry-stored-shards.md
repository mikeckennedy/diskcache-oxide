# Plan 15: the `Caches` registry uses the stored shard count on a mismatch

Plan 13, section 9, decision 4 said that in the `Caches` registry a `LayoutMismatchError` on a FanoutCache would erase the folder and re-create it with the registered settings. A first design for that (a shared/exclusive `flock` on a new lock file in every root, a guarded rename aside, a background delete and a leftover sweep) was written on 2026-11-06 and never built. In the interview the same day, Michael replaced it: the shard count is already stored in the root (the manifest), so on a mismatch the registry warns and uses what's on disk. A root created one way runs that way; to change it, delete it and start over. Nothing is ever deleted by the library.

## 1. Decisions (Michael, 2026-11-06)

- **Use the stored count, never erase.** When a registered `shards=` differs from the count a root's manifest records, the registry opens the root with the stored count and warns. The registered count applies only to a new root. To change the count of an existing cache, delete its folder while no process has it open (for example at deploy, which Talk Python's cutover already does for every cache).
- **The registry only.** A direct `FanoutCache(root, shards=8)` on a 4-shard root still raises `LayoutMismatchError` (D-032): an explicit count there is a contract, and `shards=None` is already the lenient form. No divergence entry changes; D-032 and D-038 describe direct opens only.
- **Count mismatches only.** A role mismatch (a plain `Cache` and a FanoutCache meeting in one folder, D-038) and a shard-stamp mismatch (a shard directory renamed or copied in from another root) still fall back to the reported `NullCache`, as today. Both more likely mean a mistake: two registrations pointing at one path, or folders moved by hand.
- **Loudness:** a `WARNING` with the details and how to apply the new count, each time the registry opens the cache this way, plus one `ERROR` per process per mismatch with a constant message. Sentry's logging integration turns ERROR records into events and groups them by message template, so every worker sends one event and they group into one issue.

Why this beats the erase: the library never deletes a directory, so the lock file, the extra fd per handle, the rename race, the sweep, the pre-fork caveat and the erase-loop guard all disappear. During a rolling deploy that changes `shards=`, old and new workers keep routing keys by the same on-disk count, so cross-process `add` and `incr` stay atomic and every worker keeps a real cache. With the erase, the new workers would have served a `NullCache` until the last old worker exited, and a config typo would have erased a cache's data. The cost: a changed count doesn't take effect until the folder is deleted, which the WARNING says and the ERROR makes sure someone sees.

## 2. What exists today

- `fanout::open_manifest(root, shards: Option<u32>)` (`crates/diskcache-oxide-core/src/fanout.rs`): `None` takes the stored count, or `DEFAULT_SHARDS` (8) for a new root; `Some(n)` creates a new root with `n` or, for an existing root, raises `Error::ShardCountMismatch { stored, requested }` if they differ. `open_or_create` (lines 209-226) reads the manifest or links its own; on a lost link it re-reads the winner's, and only then compares counts, so concurrent creators agree on one manifest.
- `Store::open_shards` (`store/shards.rs:38-63`) splits `max_connections` and `size_limit` by the manifest's count and refuses a total below one connection or 1 MiB per shard (`Error::InvalidSetting`, a plain `ValueError` in Python, lines 102-124).
- Every shard open calls `open::ensure_directory` on the shard's directory (`store.rs:401`), which runs `create_dir_all` on its parent, the root (`open.rs:175-179`).
- Python maps both `ShardCountMismatch` and `RoleMismatch` to `LayoutMismatchError` (`crates/diskcache-oxide-py/src/lib.rs:127`), with nothing but the message to tell them apart.
- `Caches._open` (`python/diskcache_oxide/caches.py:323-334`) constructs `FanoutCache(path, **settings)`; racing lookups each build one and the losers of `_opened.setdefault` are closed. `_resolve` turns `_OPEN_ERRORS` (`OSError`, `RuntimeError`, which covers `LayoutMismatchError`) into a reported `NullCache`; a `ValueError` for a bad setting propagates, by design (`caches.py:20-26`). `stats()`, `clear(name)` and `expire_all()` call `_open` too.
- `tests/unit/test_stubs.py:118-120` requires the stub's `FanoutCache.__new__` to list every runtime parameter, and private functions in `_core` are checked by `test_function_signatures_match` (100-102).

## 3. Design

### 3a. Core: `ShardRequest`

`open_manifest(root, request: ShardRequest)` replaces the `Option<u32>`:

```rust
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ShardRequest {
    /// The stored count, or `DEFAULT_SHARDS` for a new root (today's `None`).
    Stored,
    /// Exactly this count: a new root gets it, an existing one must match (today's `Some(n)`).
    Exactly(Manifest),
    /// This count for a new root, the stored count for an existing one: never a count mismatch.
    ForNewRoot(Manifest),
}
```

The variants hold a validated `Manifest`, so the range check runs once, where the request is built (`shard_count` in the extension), not inside `open_manifest`. `ForNewRoot(m)` is "create with `m`, skip the compare" inside the existing create-or-agree step: whatever manifest the first `link` placed is the answer. A lost link whose winner then vanishes is already `ManifestInvalid` (a `FormatMismatchError`, so a reported `NullCache`), and a root deleted before the temp file is created is `Io(NotFound)` (an `OSError`): neither re-creates the root with a guessed count. `refuse_plain_cache` and the shard-stamp checks run unchanged, so D-038 and stamp mismatches still raise. (A "try `Exactly`, then retry with `Stored`" design would have created `DEFAULT_SHARDS` on a root deleted between the two calls, which is why the decision is one call.)

### 3b. Core: settings that only fit the registered count (review R1)

Under `ForNewRoot(m)`, `Store::open_shards` (or a check before it) first validates the `max_connections` and `size_limit` splits against the *registered* count `m`, before anything touches disk, so a setting that is wrong for the registration raises `ValueError` whatever is on disk, as a bad setting always has. If the splits are valid for `m` but not for the stored count (`register('rl', shards=4, size_limit=4 * 2**20)` on an 8-shard root, or `max_connections=4` on a 16-shard root), the open fails with a new `Error::StoredShardsRefuseSettings { stored, requested, reason }`, mapped through `layout_mismatch` in `to_py_err`. The registry then serves its reported `NullCache`, with a message that names both counts, the setting, and says to delete the folder or raise the setting. Without this, the change would turn today's reported `NullCache` into a `ValueError` raised inside a request, depending on what's on disk.

### 3c. Core: shard opens never create the root (review R3)

Today a shard open creates its parent directories, so if a deploy's `rm -rf` lands after the manifest step but before shard k opens, shard k re-creates the root: with umask permissions instead of 0700, without a manifest, and holding a shard stamped with the old count. The next opener then writes a new manifest and trips shard k's stamp, and the cache stays a `NullCache` until the folder is deleted again. This predates plan 15, but plan 15 makes "delete the folder" the documented way to apply a count, so it's fixed here: for `Role::Shard`, the directory is created with `DirBuilder::new().mode(0o700).create(dir)` (not recursive), accepting `AlreadyExists`. A vanished root then gives `NotFound`, an `OSError`, so a reported `NullCache` and a retry. The root itself and plain caches keep `ensure_directory`'s recursive create (renamed to say so, now that shards no longer use it).

### 3d. Extension: a private constructor function (review R2)

Not a private keyword on `FanoutCache`: the stub test requires `__new__`'s stub to list every runtime parameter, so a private flag would appear in the public signature, the rendered docs and IDE completion, and `ty` would reject it in `caches.py` otherwise. Instead:

- The body of `FanoutCache::new` (`crates/diskcache-oxide-py/src/cache/fanout.rs:80-100`) moves into `fn construct(py, directory, ..., request: ShardRequest) -> PyResult<FanoutCache>`. `#[new]` passes `Stored` or `Exactly`.
- A private `#[pyfunction] _open_fanout_keeping_stored(directory, timeout=None, *, shards: int, size_limit=None, ...)` passes `ForNewRoot`. It takes every `FanoutCache` setting, with `shards` required. It is in `_core.pyi` (private, so not rendered) and checked by `test_function_signatures_match` like `_encode` and `_gate_stats`.
- A test asserts that its parameters equal `inspect.signature(FanoutCache)`'s apart from `shards`'s default, so a future `FanoutCache` setting can't be left out of it.

### 3e. Registry

In `Caches._open`, for a name registered with `shards=n` where `n` is an int:

1. Construct `_open_fanout_keeping_stored(path, **settings)` (with `shards=n` among them). A registration with `shards=None` constructs `FanoutCache(path, **settings)` exactly as today and never warns.
2. Put it through `_opened.setdefault` as today. Only when `winner is cache` (this call's handle won) and `cache.shards != n`, log (section 5). Losers are closed and log nothing, so racing lookups on 3.14t log one WARNING, not one per thread (review R4).
3. Return the winner.

`stats()`, `clear(name)` and `expire_all()` get the same handle through `_open`, and log the same way on a real open only. `CacheStats` doesn't change; the shard count is on the handle (`caches.fanout(name).shards`).

The registry's setting check excludes every parameter whose name starts with `_` from `_CACHE_SETTINGS` and `_FANOUT_SETTINGS` (`caches.py:61-62`). With 3d there is no such parameter on `FanoutCache`, so this is only a guard against a future one, kept with its `TypeError` test.

## 4. "Once per process" for the ERROR

A module-level set in `caches.py`, `_REPORTED_SHARD_MISMATCHES`, keyed by `(realpath(directory), stored, registered)`, guarded by a `threading.Lock` (3.14t), not a set on the registry: an app that rebuilds `Caches` (a reload, tests, one registry per worker thread) would otherwise report again. The key re-reports after a later deploy changes the registered count again, which is wanted. A child forked from a process that already reported inherits the set and stays quiet; that's still one Sentry issue. `close_all()` doesn't touch it. Tests clear it through a private helper.

## 5. Logging

- **WARNING, each open that falls back:** `'cache %r at %s has %d shards but is registered with shards=%d; using the %d on disk. To apply shards=%d, delete %s while no process has it open'` (name, path, stored, registered, stored, registered, path). On the `diskcache_oxide` logger, like the registry's other messages.
- **ERROR, once per process per mismatch (section 4):** `'a FanoutCache is registered with a shard count its folder does not have'`, a constant message with no `%` arguments, plus `extra={'cache': name, 'directory': path, 'stored_shards': stored, 'registered_shards': n}`. The `extra` keys must never clash with `LogRecord` attributes (`name`, `msg`, `args`, `module` and the rest); a test pins the keys.
- Not through `on_open_error`: the open succeeded, and an app's reporter would treat it as an outage. The R1 case (`StoredShardsRefuseSettings`) is a failed open and goes through `on_open_error` like any other.

## 6. Tests

Core (`fanout.rs` and `store/shards.rs`):

- `ForNewRoot(m)` on a missing root creates `m`; on an existing root with another count returns the stored manifest and writes nothing (the manifest's bytes and mtime unchanged, as D-019 asks).
- A mixed race, as `racing_creators_agree_on_one_manifest` (`fanout.rs:577-612`) does with threads and a barrier: `Exactly`, `Stored` and `ForNewRoot` callers with different counts on a new root. `Exactly` losers get `ShardCountMismatch`; every `ForNewRoot` and `Stored` caller gets the winner; no temporary files are left.
- `ForNewRoot` on a plain cache's directory still raises `RoleMismatch`.
- R1: `size_limit` and `max_connections` each valid for the registered count but not the stored one give `StoredShardsRefuseSettings`; invalid for the registered count gives `InvalidSetting` whatever is on disk.
- R3: the root deleted between `open_manifest` and `open_shards` gives an I/O `NotFound`, and the root is not re-created.
- `Exactly` and `Stored` keep their current tests unchanged.

Python (`tests/unit/test_caches.py`):

- `test_a_layout_mismatch_is_a_null_cache_reported_remembered_and_retried` loses its `_wrong_shard_count` parametrization (that case now opens a real cache); `_plain_on_a_root` and `_plain_on_a_shard` stay (review R5).
- An 8-shard root registered with `shards=4`: the lookup is a real FanoutCache with `shards == 8`, the existing entries are still there, one WARNING and one ERROR with the expected messages and `extra`.
- A reopen after `close_all()`, and a second `Caches` on the same base directory: a WARNING again, no second ERROR. A later registration with yet another count: a second ERROR.
- Racing lookups from 8 threads on `.venv-ft`: one WARNING, one ERROR, one handle kept.
- A new root registered with `shards=4` gets 4 shards and logs nothing; `shards=None` on an existing root logs nothing.
- R1 through the registry: a reported `NullCache` whose message names both counts; a setting invalid for the registered count still raises `ValueError` from the lookup.
- `stats()`, `expire_all()` and `clear(name)` on a mismatched registration return real numbers and log one WARNING.
- A role mismatch (D-038) is still a reported `NullCache`; a direct `FanoutCache(root, shards=4)` on an 8-shard root still raises `LayoutMismatchError` (the D-032 test stays as it is).
- `register(..., _anything=...)` raises `TypeError`.
- `_open_fanout_keeping_stored`'s parameters match `FanoutCache`'s apart from `shards`'s default.
- Cross-process: the spawn-context `_race` harness in `tests/unit/test_fanout.py:767-806` moves into a shared helper. 8 spawned racers, alternating `shards=8` and `shards=16`, open a new root through `Caches` at a barrier; each `incr('k')` and reports its `cache.shards` and whether it logged the WARNING and the ERROR. Every child got the manifest's count; exactly the children whose registered count differs from the winner's logged, one ERROR each; the parent's `FanoutCache(root).get('k') == 8`; the root holds only the shard directories and the manifest. `@pytest.mark.timeout(120)`, as the existing race test has.

Soak: none. The open path changes only for the registry's lenient case and for the shard directory create, which the soak's `fanout` role exercises on every open.

## 7. Docs

Each place that says a count mismatch is a `NullCache` in the registry:

- `caches.py`: the module docstring (lines 20-23), the `_OPEN_ERRORS` comment (64-68), the `register` docstring (192-198: an existing root keeps its count) and the `fanout()` docstring (276-279).
- `user_guide/caches.qmd:57`, `user_guide/fanoutcache.qmd:59`, `user_guide/errors.qmd:124`, and the errors page's log-lines section, which gains the new WARNING and ERROR.
- `skills/diskcache-oxide/SKILL.md:67`.
- How it plays with a rolling deploy: if the folder is deleted while old workers still run, whichever process re-creates it first sets the count, so an old worker that reopens can re-create it with the old count, and the new count waits for the next deletion. Safe, but not what people expect, so the guide says to delete it with every worker stopped.
- The CHANGELOG (under Changed). Then `make docs` and commit `site/`.
- Plan 13, section 9, decision 4: marked as replaced by this plan (done).

## 8. Build order

One step: 3a to 3e, sections 4 and 5, the tests and the docs. `the-rust-architect` reviews the result.

## 9. Review

- 2026-11-06, `the-rust-architect` on the first version: PASS with required changes, all folded in above. R1: settings split by the count now give a layout error (a reported `NullCache`) when they fit only the registered count, and a `ValueError` when they don't fit it (3b). R2: a private `_core` function instead of a private keyword, since the stub test would put the keyword in the public signature (3d). R3: shard opens no longer create the root, closing a window in which a deletion re-created it without a manifest (3c). R4: only the winner of `_opened.setdefault` logs (3e). R5: the old `_wrong_shard_count` test case moves (section 6). R6: the docs list (section 7). Also taken: the enum holds a validated `Manifest`; "once per process" is a module-level set keyed by path and both counts (section 4); the cross-process test's shape; the mixed-request core race; the rolling-deploy note.

## 10. Progress

- 2026-11-06: built in one step. Core: `fanout::ShardRequest` (`Stored`, `Exactly(Manifest)`, `ForNewRoot(Manifest)`) replaces `open_manifest`'s `Option<u32>`, with the compare running only for `Exactly`; `Store::check_shard_settings(base, shards)` runs the `max_connections` and `size_limit` splits without touching disk; `Error::StoredShardsRefuseSettings { stored, requested, reason }` maps to `LayoutMismatchError`; shard opens call a new `open::ensure_shard_directory` (0700, never creates a parent) while plain caches and roots keep `ensure_directory` (not renamed, since its many test callers are all plain or root directories). Where the build differs from section 3: the R1 orchestration (check the settings against the registered count before touching disk, then against the stored count once the manifest is known) lives in the extension's `open()` in `cache/fanout.rs`, beside the connection-total default it depends on, not in `Store::open_shards`, so the R1 cases are tested through the registry plus a core test of `check_shard_settings`. Extension: `FanoutCache::construct(..., keep_stored)` shared by `#[new]` and the private `_open_fanout_keeping_stored` (in `_core.__all__` and the stub, checked by `test_function_signatures_match`). Registry: `_open` uses it for an int `shards=`, logs only when its handle won `_opened.setdefault`, and keeps the module-level `_REPORTED_SHARD_MISMATCHES` set (with a lock and a test hook); `_public_settings` drops `_` parameters. Tests: core (`for_a_new_root_creates_its_count_and_otherwise_keeps_the_stored_one_untouched`, `for_a_new_root_still_refuses_a_plain_caches_directory`, `mixed_requests_racing_on_a_new_root_all_agree_on_the_winner`, `the_settings_check_matches_what_opening_refuses`, `a_root_deleted_before_its_shards_open_is_not_re_created`), and in `tests/unit/test_caches.py` the section 6 list, with the cross-process race on the shared `tests/unit/spawn_race.py` (moved out of `test_fanout.py`). The `_wrong_shard_count` case left the NullCache test. Docs: the `caches.py` docstrings and comment, `user_guide/caches.qmd`, `fanoutcache.qmd`, `errors.qmd` (the entry and the log lines), the skill and the CHANGELOG (Changed, and Fixed for R3). Green: clippy, `cargo test` (271 core), the suite against oxide on 3.14 and 3.14t (1532 each), conformance against diskcache.
- 2026-11-06: the architect's review of the build: PASS with required changes, all applied. `check_shard_settings`'s doc comment had been spliced into `open_shards`'s; each now sits on its own function. A registry test of `shards=2` on a 16-shard root pins that the stored count, not the registered one, picks the default connection total. A core test pins shard directories at 0700 (D-015) and a file at `000` as an error. The ERROR dedupe drops its `threading.Lock` for `dict.setdefault`, so a lock held by another thread at `fork()` can't hang a child's lookup. Also taken: the thread race is forced (every thread builds a handle before any reaches `setdefault`, and the test asserts all 8 did); a symlinked base reaches the same folder and logs one ERROR; `open_until`'s two role branches are one. Known and left alone: `spill.rs` creates a shard's value-file directories recursively, so a process still holding a shard open after its root was deleted re-creates `root/000/aa/bb` on its next large write. That's outside the supported "delete with no process open" case and benign: the next opener writes a manifest and fresh shard databases, leaving an orphan value file.
