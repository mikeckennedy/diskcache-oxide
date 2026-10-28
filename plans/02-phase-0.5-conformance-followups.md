# Phase 0.5: Conformance follow-ups

Parent: [00-roadmap.md](00-roadmap.md). Follows [01-phase-0-conformance.md](01-phase-0-conformance.md).

Status: plan reviewed by the architect (round 1 incorporated).

## Goal

Close the gaps the diskcache-community fork and the upstream issue tracker exposed, before any Rust exists. Every new test is verified against diskcache 5.6.3 first.

- **Deterministic failure on diskcache:** when diskcache fails a scenario we've decided to fix, the test is a strict-xfail divergence on diskcache, plus a `backend_only('oxide')` test of the fixed behavior.
- **Probabilistic failure on diskcache:** when diskcache only fails sometimes, the test is `backend_only(...)` for the backends that must pass it. Following the D-012 precedent, the divergence entry records diskcache's measured failure rate, and the bug log gets a repro.

No Rust in this phase.

## Harness changes (step 1)

- **Community backend (Item H).** Add `CommunityBackend` to `backends.py`: `import diskcache_community`, database `cache.db`, its own `Timeout`, and `spill_threshold` observed the same way as for diskcache. Steps:
  - Check the PyPI name, and that installing it doesn't add a `diskcache` module that shadows 5.6.3.
  - Put it in its own `community` dependency group.
  - Add a session-start assertion that `diskcache.__version__ == '5.6.3'` whenever the diskcache backend is selected.
- **Clock patching becomes a backend attribute.** `Backend.patches_time_time: bool` is True for diskcache and community, and False for oxide. `fake_clock` uses that attribute instead of checking `backend.name == 'diskcache'` (conformance/conftest.py:~105).
- **`backend_only` accepts several backends:** `backend_only('oxide', 'community')`. That way the D-012 fork test and other "fixed in the fork" tests also run against community.
- **Per-backend fd budget in the adapter:** `Backend.fd_budget(threads)` gives the maximum number of open fds inside the cache directory with that many live threads. diskcache's budget is proportional to the thread count, and oxide's is about 2 × pool size + 1.
- `--backend=community` runs the whole suite. `diverges` markers with `backend='oxide'` don't apply to community, so any place where the fork differs from 5.6.3 shows up as a failure. Those differences go in a short "Community fork differences" section of `DIVERGENCES.md`, and we don't add markers for them. Expected differences: D-012, D-015 (0700), D-017.

## Tests

**Test A: a reader racing an overwrite of a spilled value** (upstream #258, #294). File: `test_processes.py`.
- One writer process overwrites a single key over and over with values of at least 2 × `spill_threshold`, each carrying a version and a checksum. Several reader processes `get` it in a loop with a sentinel default object. There are no deletes.
- Use a new reader target: the existing `spill_reader` counts `None` as a miss, and that's exactly the failure we're looking for.
- Requirement: every read returns a valid value, old or new. Never the sentinel, never `None`, never an exception.
- Final checks after the writers stop: `key in cache`, `len(cache) == 1`, and `len(spill_files) == 1`. These catch a reader that deletes the live row (the D-013 compare-and-delete hazard) and orphaned files.
- On diskcache the failure is expected to be probabilistic, so the test is `backend_only('oxide', 'community')` if community passes it. Divergence **D-016** (a key that's present the whole time never reads as missing or torn while its value is being overwritten) records diskcache's failure rate, and the bug log gets a repro.

**Test B: many processes open a brand-new directory at the same moment** (upstream #266, #349). File: `test_processes.py`.
- 8 spawned processes start on a barrier, each opens `Cache(new_dir)`, then each does a `set` and a `get`. Repeat with 8 threads in one process.
- Requirements:
  - no exception anywhere, including from the `journal_mode=WAL` switch racing between openers;
  - every value is readable afterwards;
  - `integrity_check` is ok (run in the helper subprocess);
  - on oxide, the directory is 0700 even with racing creators (D-015).
- If diskcache fails deterministically, it's a strict xfail. If it fails only sometimes, follow the probabilistic rule above.

**Test C: two Cache instances on one directory, then a fork** (upstream #325). File: `test_processes.py`, in the subprocess-isolated fork style.
- In the parent, `a` and `b` are open on the same directory and both have been used.
- **Variant 1:** the process forks while idle. In the child, `a` and `b` both read and write. The parent keeps working.
- **Variant 2:** a background thread is in the middle of writing through `b` at the moment the main thread forks with `a`. This proves the fork barrier covers the whole process, not just one `Cache`. It's probabilistic on diskcache, so it runs `backend_only('oxide', 'community')`.
- Afterwards, `integrity_check` is ok.

**Test D: connections and fds are bounded, and closing leaves nothing behind** (upstream #295, #336). File: `test_threads.py`.
- **Peak:** N = 64 threads (kept under macOS's default soft limit of 256 fds) hold at a barrier after each makes one `get` and one `set` on a shared `Cache`. While all of them are alive, count the open fds whose path is inside `cache_dir`. Resolve paths with `os.readlink('/proc/self/fd/N')` on Linux, and `fcntl.fcntl(fd, fcntl.F_GETPATH)` on macOS. Poll until the count stops changing (with a deadline) rather than sampling once.
  - Requirement: peak ≤ `backend.fd_budget(N)`.
  - diskcache keeps one connection per thread, so it will exceed oxide's budget. That's divergence **D-017** (the connection count is bounded by the pool, not by the number of threads alive): a strict xfail on diskcache, plus a test for oxide.
- **After `close()`:** once the threads exit and `close()` is called, the count of fds inside the cache directory is 0.
- **No ResourceWarning:** run the scenario in a subprocess with `-W error::ResourceWarning`, then check for an exit code of 0 and no "ResourceWarning" in stderr. Since 3.13, sqlite3 reports these from dealloc as unraisables on the exiting thread, and `catch_warnings` is context-local on 3.14t, so an in-process check can't see them. If diskcache fails, it's divergence **D-018** (no unclosed-connection warnings).

**Test E: opening and reading never modify the database.** File: `test_basic.py`.
- **Fixture:** write entries with no expiry and no spill files that might go missing, so reads have no legitimate reason to write. Then close every handle.
- **Precondition:** the WAL file is absent or empty. Checkpoint with the helper if necessary.
- Snapshot the main database file's hash, size and mtime. Never hash the `-shm` file, and don't run `integrity` between snapshots.
- Start a `SqlitePeer` in another process *before* reopening, and read its `data_version`.
- Reopen with the same settings. Do `get`, `in`, `len`, `iterkeys` and `volume`, then close.
- Requirements: the peer's `data_version` is unchanged, and the main database file's hash and mtime are unchanged.
- diskcache's `__init__` rewrites its Settings, so it's expected to fail. That's divergence **D-019** (opening with unchanged settings never writes): a strict xfail on diskcache, plus a test that runs on oxide and community.

**Test G: the bundled SQLite and fork-after-use.** File: `test_processes.py`.
- **Smoke test:** the parent uses a cache, then forks. The child opens a *new* `Cache` on a different directory and uses it, and exits with 0. This Python's stdlib sqlite3 is SQLite 3.53.1 compiled in, not Apple's libsqlite3, so this is mostly a smoke test here.
- **Oxide-only check:** diskcache-oxide reports its SQLite version and compile options (e.g. `diskcache_oxide.sqlite_version()`), and they match the bundled build. This protects against accidentally linking the system SQLite.

**Test I: a writer killed in the middle of spill writes** (deploy restarts do this). File: `test_processes.py`.
- A writer process writes and overwrites spilled values in a loop and is sent SIGKILL at a random point (several rounds).
- Afterwards: `integrity_check` is ok, every key reads as a valid hit or a miss, nothing raises, and the cache accepts new writes.
- Orphaned files left by the kill are allowed. Phase 6a's `check()` will clean them up. Record how many there are.

**Moved out of this phase: Test F (forking inside your own transaction).** CPython ignores an exception raised in an `os.register_at_fork` before-hook and forks anyway (verified on 3.14.10 and 3.14t), so diskcache-oxide can't refuse the fork. Instead, the behavior goes into the Phase 1 fork-barrier design. The barrier waits only for *other* threads' transactions. In the child, the inherited transaction is poisoned: every operation inside it and its `__exit__` raise `RuntimeError`, and nothing commits. The cache works normally on fresh connections outside it. The parent continues and commits normally. The before-hook may also emit a `RuntimeWarning`. diskcache's behavior in this case goes into the bug log as a repro, not a strict test. The oxide test lands with `transact` in Phase 6b.

## Bug log

Any new diskcache bug gets an entry and a standalone repro in `upstream/`, in the same format as the existing entries. Row 7 (the missing value file) now points to D-013.

## Steps (one at a time)

1. Harness changes and the community backend. Run the existing suite with `--backend=community` and record the differences.
2. Tests A, B, C, G and I (processes), each verified on 3.14 and 3.14t, against diskcache and against community.
3. Test D (fds and threads).
4. Test E (opening and reading never write).
5. `DIVERGENCES.md` (D-016 to D-019, plus the community differences) and `upstream/` updates, the full suite 3× on 3.14t, and the `slow` tests.
6. Architect end-of-phase review, then fixes, then commit and push.

## Exit criteria

- The full suite passes on 3.14 and 3.14t against diskcache, in under about 20 s (not counting `slow` tests).
- `--backend=community` runs end to end, and its differences from 5.6.3 are listed.
- Each new divergence has an entry, a test on each side (or a recorded failure rate and a repro, for probabilistic ones), and a bug-log entry for bugs.
- Nothing is flaky across 3 runs on 3.14t.

## Out of scope

Rust code, the Phase 1 spikes, and Test F's oxide test (it lands with Phase 6b).

## Results (Phase 0.5 complete, pending architect review)

**Suite on diskcache:**
- 250 passed, 84 skipped (oxide-only or not yet supported), 7 xfailed.
- About 12-13 s on 3.14 and 3.14t, stable across 3 runs on 3.14t.
- The `slow` tests pass on both builds.

**Community backend:** 4 tests fail on the fork, all expected and listed in `DIVERGENCES.md`:
- D-015: the fork creates new directories as 0700;
- D-017: the fork still keeps one connection per thread (131 fds at 64 threads);
- D-008, twice: the NaN bug is still in the fork.

The fork passes D-012 (its fork gate works, including across two `Cache` objects), D-018 and D-019.

**What each test found:**

| Test | diskcache 5.6.3 | community 5.8.1 | Divergence |
|---|---|---|---|
| A: reading a spilled value while it's overwritten | reads as missing in 15-45% of runs (100% in the heavy variant) | same bug | D-016 (`covers`, oxide-only) |
| B: racing openers of a new directory | pass | pass (and 0700) | none |
| C1: two `Cache`s on one directory, then an idle fork | pass | pass | none |
| C2: fork while a thread writes through the second `Cache` | child times out in 13/25 to 15/25 runs | pass | D-012 |
| D: peak fds at 64 threads | 131 (grows with threads) | 131 | D-017 |
| D: fds after `close()` | 130 left open until gc | 0 | D-018 |
| D: ResourceWarning | 64 warnings | none | D-018 |
| E: opening and reading never writes | `data_version` 2 → 3 | pass | D-019 |
| G: fork, then a new `Cache` in the child | pass | pass | none (oxide's bundled-SQLite check waits for Phase 1) |
| I: writer killed with SIGKILL mid-spill | consistent; 0-5 orphan files per 6 kills | same | none (orphans are cleaned by `check()` in Phase 6a) |

**Harness changes:**
- `CommunityBackend`, a `community` dependency group, and a 5.6.3 version guard.
- `patches_time_time`, multi-backend `backend_only`, and `fd_budget`.
- A new `covers('D-NNN')` marker that ties probabilistic divergences to their oxide-only tests, checked by the registry.
- The D-012 fork test's holder now commits on its own after 0.3 s. Before, it waited for the fork, which deadlocks against any correct fork barrier, including the community fork's and oxide's planned one.

**Bug log:** three new entries with repros, ten in total, all of which reproduce:
- #8: the spilled-value read race (upstream #294);
- #9: `close()` leaves other threads' connections open (related to #295 and #336);
- #10: opening with unchanged settings writes.

**Moved to Phase 1:**
- Test F's design (the poisoned inherited transaction).
- The bundled-SQLite introspection API that Test G expects.
