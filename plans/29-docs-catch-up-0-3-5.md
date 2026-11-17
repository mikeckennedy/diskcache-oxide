# Plan 29: bring the docs up to 0.3.5

Status: in progress (2026-11-16). Steps 1-4 and 6 done, not committed: lint, ty, the unit suite on 3.14 and 3.14t, and `make docs` pass. Step 5 waits for a quiet studio. A grep for the old claims after the step-1 edits also found "shortest interval wins" in the `maintenance_interval` docstring of `_core.pyi`, and `0700` stated without "on Unix" in D-015 (both files), getting started, troubleshooting, web workers and the `directory` docstring; both are fixed. Reviewed once by the-rust-architect (no wrong claims; eight imprecise ones tightened, two gaps added, folded in below). Comes from a full audit of the docs against the 0.3.5 release on 2026-11-16: every page in `docs-source/`, the README, the skill and `examples/webapp`, checked against `_core.pyi`, `caches.py`, `null.py`, `protocol.py`, the Rust sources and `tests/DIVERGENCES.md`. The 0.3.5 features are documented where they were built, but older pages still describe the cache before background maintenance (plan 28), a few recipes drift from the recommended production setup, and some pages never got the new settings.

## Scope and tier

All docs, plus `CHANGELOG.md`, `tests/DIVERGENCES.md` (D-024's text), one docstring in `python/diskcache_oxide/caches.py` and one in `_core.pyi`. No behavior changes. The docs, changelog, divergence text and the stub are release tier None (`scripts/release_tier.sh`'s code pattern matches `.py$`, not `.pyi`). The `caches.py` docstring is Patch by the script; it ships with the next release that ships code rather than forcing a 2-hour soak for a docstring.

Out of scope: the doc comment at `crates/diskcache-oxide-core/src/background/worker.rs:14-15` also leaves out that the fork waits for a folder sweep in progress. Editing anything under `background/` puts a change in the Full tier, so fix it with the next Full-tier change there.

Every change follows the docs rule: each claim must match the code and the tests. A step that rewrites a behavior claim cites the code it was checked against in the commit message.

## Steps

Each step ends with `make docs` and a look at the changed pages in `make docs-serve`. CLAUDE.md allows commits only at the end of a phase, so the steps commit once at the end unless Michael asks for one commit per step (question 5). Steps are ordered by how much harm the current text does.

### Step 1: correct what's wrong

Claims the 0.3.5 code contradicts.

1. **"Opening and reading never write."** Since D-061, background maintenance writes when it has work and once an hour per cache to coordinate, and `maintenance_interval=0` keeps a process write-free. Update D-019 on the divergences page (its summary row and body, which still say "never write"; `tests/DIVERGENCES.md` already has the right text), `switching-from-diskcache.qmd` ("Opening a cache and reading from it never writes"), `concurrency-and-forks.qmd` ("Opening never writes") and `roadmap.qmd` ("a cache with nothing to do is only read").
2. **How `maintenance_interval=0` combines.** `storage-and-wal.qmd` says "the shortest interval any open handle on the directory asked for wins". In `background/schedule.rs`, a handle asking for 0 doesn't count (`schedule.rs:181-183`, `store.rs:886`), and the interval never lengthens while the store lives (`schedule.rs:122-123`). The store remembers asks from handles that have since closed, and resets only when every handle has closed (`revive`, `schedule.rs:212-233`). The read-only-replica advice (here, in D-061 and in the skill) becomes: "maintenance is off for a cache in a process only when every handle opened on it there asked for 0; an earlier handle that asked for an interval keeps it on until every handle on the cache has closed."
3. **Scheduled maintenance.** `errors.qmd` ("Run them from one scheduled job rather than from every worker") and `troubleshooting.qmd` ("Run maintenance from one scheduled job") still say to schedule it. `web-workers.qmd`'s Maintenance section leads with "Run maintenance from one place". Each should say that background maintenance does this, and that explicit `expire()`, `cull()`, `clear()` and `checkpoint()` are for when you want it now, with their own `timeout=`.
4. **Expired entries in counts.** `work-queues.qmd` says "Items that expired but haven't been removed yet are counted too". D-060 hides them from `iterkeys()`; drop the clause.
5. **The site-caches recipe's shutdown.** `site-caches.qmd` calls `caches.expire_all(timeout=10)` in `close()` and says "`close()` removes expired entries". Maintenance does that already; remove both.
6. **D-024 and the WAL.** The divergences page and `tests/DIVERGENCES.md` don't say that maintenance checkpoints a log grown past half of `wal_size_limit`, and the test file's D-024 still says `manual` mode needs `auto` or a periodic `checkpoint()`. Soften that sentence rather than delete it. The background checkpoint is PASSIVE: it never waits for readers, and truncates only when everything was copied, nothing was busy and nobody is waiting (`store.rs:1656-1663`, 1692-1708). So with busy readers in other processes it copies what it can each interval, but doesn't keep the WAL bounded, and `auto` or an explicit `checkpoint()` is still the answer there. On a default `FanoutCache` shard (two connections), it runs only when neither connection is leased at that moment.
7. **The changelog.** The 0.3.5 D-060 bullet says "a scheduled `expire()` is still how a mostly-read cache frees space", against the same entry's "There's no longer an `expire()` to schedule". Correct it (see question 2).
8. **`caches.py`'s module docstring** lists "when to run `expire_all()`" among what stays in the app. Drop it.
9. **Missing `from typing import Optional`** in the executed cells of `caching-http-responses`, `custom-types`, `sessions`, `single-use-keys` and `work-queues` (left by 35102e3's `X | None` change). The build passes only because 3.14 evaluates annotations lazily; copied code fails ruff (F821), and anything that reads the annotations gets a `NameError`.
10. **`_core.pyi:703`** says `FanoutCache.maintenance_stats()`'s `last_error` is the first shard's. It's the error of the first shard, in shard order, that has one (`cache.rs:801-803`).

### Step 2: recipes follow the recommended setup

The setup: rebuildable data in a durable `Caches` registry with `timeout=1.0`; state (sessions, claims, locks, queues, counters, tokens) in plain caches opened directly, eviction off, never durable, under `/var/lib/my-app`.

1. **`counters.qmd`** says to keep counters in a cache "which `Caches` lets you do per name", but the registry hands out a `NullCache` on a failed open (so `incr` silently returns `default + delta`) and the recommended registry is `durable=True`, which the next line says drops counts. Open counters directly as state.
2. **`single-use-keys.qmd`** registers tokens in `Caches` and says `durable=True` is safe. Open them directly as state, link "State isn't cache", and drop or reframe the `durable=True` paragraph.
3. **`run-once.qmd`'s first sample** puts the claim on the main cache, which the same page later says not to do. Start with the coordination cache.
4. **`coordination.qmd`'s lock example** uses `/var/cache/my-app/locks`, inside a cache directory, against the page's own advice. Use `/var/lib/my-app/state/coordination`. Also "two-character subdirectories" should be "two-hex-character".
5. **The old `/var/cache/my-app` path** remains in `asyncio.qmd`, `upgrading.qmd` and the skill. Move them to `/var/lib/my-app/...`.
6. **`upgrading.qmd`'s cleanup** says `caches.remove_old_formats()` covers every registered cache, but state caches aren't registered. Add the loop over the state caches, as `examples/webapp/app/deploy_cleanup.py` and `docker.qmd` do.
7. **The skill's decision table** sends "counters, rate limits, claims" to `FanoutCache(shards=8)`, against its own "plain caches, not FanoutCache, unless..." and the setup's plain coordination cache. Reword to "one hot cache many processes write".

### Step 3: fill the gaps

1. **`concurrency-and-forks.qmd`**: the process's one maintenance thread (native, never takes the GIL; `worker.rs:4-5`). The fork barrier stops and joins it first, so a fork waits for the chunk, checkpoint or folder sweep in progress (a PASSIVE checkpoint has taken up to 1.4 s; the sweep is bounded at 100 ms by `SWEEP_BUDGET` and doesn't check for a stop). After the fork the thread restarts in parent and child if a live store wants maintenance (`worker.rs:199-210`), and the child's counts start from zero (turns, removed, failures, `last_error`; `interval` keeps its value, `background.rs:212-224`). A C-level `fork()` gets no stop (D-061). The last `close()` waits for a turn in progress, at least 50 ms and otherwise within its own deadline (D-018, `store.rs:433-439`); the same half-sentence goes in `storage-and-wal.qmd`.
2. **`fanoutcache.qmd`**: each shard gets its own turn, the shards get the hourly folder sweep, and `maintenance_stats()` sums turns, removed and failures across shards, with `interval` the largest and `last_error` from the first shard, in shard order, that has one; failed thread spawns count in `failures` too (`cache.rs:797-813`). A `clock=` veto applies per database file, so to each shard. Rename "Maintenance: one budget, partial failures" to "Maintenance calls: ..." so it isn't read as background maintenance.
3. **`caches.qmd`**: `maintenance_interval` on `Caches(...)` and `register(...)`; `maintenance` in the `CacheStats(...)` comment; `remove_old_formats` in the "works the same for both kinds" list.
4. **`errors.qmd`**:
   - The maintenance-failure WARNING, exactly `background maintenance failed on the cache at <dir>: <error>` (`cache.rs:1160`). It's logged once per streak of failures, by the next single-key call on the cache, so a cache nobody calls never logs it (`cache.rs:1141-1146`). The line doesn't mention `maintenance_stats()`, so the page points there itself (`failures`, `last_error`). In a starvation streak `failures` rises by one every 3 intervals; only the log is once per streak.
   - Both `maintenance_interval` `ValueError`s: `maintenance_interval can't be used with clock=: ...` (`cache.rs:866-869`, checked first) and `maintenance_interval must be 0 (off), or from 1 to 86400 seconds (a day), got ...` (`cache.rs:876-878`, also for NaN and infinity); a bool or non-number raises `TypeError` (`cache.rs:2712-2716`).
   - `UnknownFileWarning` and `EmptyDirWarning`, since the page promises every exception.
   - The real `cache %r at %s is busy; using a NullCache, retrying in %g s: %s` text for a timed-out registry open (`caches.py:463-465`), logged only when no `on_open_error` is set. `troubleshooting.qmd` should tell readers to look for it too.
5. **`switching-from-diskcache.qmd`**: rows for D-060 and D-061 in the comparison table (the change that matters most to anyone running `expire()` from cron); `maintenance_interval` and `raise_on_mismatch` under the new settings; background maintenance under what diskcache doesn't have.
6. **`null-cache.qmd`**: rows for `maintenance_stats()` (all zeros) and `checkpoint()`; name `maintenance_stats()` and `remove_old_formats()` where it says what a `CacheLike` fake needs.
7. **`getting-started.qmd` and the testing recipe**: passing a nonzero `maintenance_interval` with `clock=` raises (leave it out or pass 0; left out, the handle just vetoes maintenance, `cache.rs:856-869`), and one `clock=` handle turns maintenance off for that directory in the whole process until every handle on it closes. A new cache's first turn comes at least one interval after it opens (`schedule.rs:9-10`), so a test shorter than 5 minutes never sees maintenance at the default; `maintenance_interval=0` is for long runs, or suites that set a short interval, to keep `expire()`/`cull()` results and `volume()` steady under assertions. A `CacheLike` fake needs `maintenance_stats()`.
8. **`monitoring.qmd`**: `maintenance_stats()['failures']` in the `cache_health()` dict and a gauge in the Prometheus collector (`None` when unreadable).
9. **`storage-and-wal.qmd` precision**: "two rows" in `settings`, not one; a failure is logged once per streak; the folder sweep runs hourly or every interval if that's longer; the checkpoint is skipped when the WAL is within half of `wal_size_limit`, when it would take the last free connection (always with `max_connections=1`; `store.rs:1682-1685`), while the request path is backing off, and when a writer in this process is waiting; the `clock=` veto lasts until every handle on the directory closes; the eviction bullet says maintenance also trims a cache left over `size_limit`, `cull_limit` rows at a time, never under `eviction_policy='none'` or with `cull_limit` 0 (`turn.rs:225-232`). Line 140's "if a request holds the write lock, or one is waiting for it, the turn steps aside" needs "in this process" (`write_waiters` is this process's lock, `store.rs:2039-2043`).
10. **Maintenance cost to requests**, in `performance.qmd` (new line), `storage-and-wal.qmd:140` and D-061 (both pages and `tests/DIVERGENCES.md`): "a request in the same process waits at most one chunk (about 130 µs for 100 small rows); a writer in another process can wait for a few chunks, never past the turn's 50 ms." The current "at most one chunk late" holds only within a process: a writer elsewhere meets SQLite's lock and can lose the race against back-to-back chunks.
11. **README**: a line on background maintenance, as `index.qmd` has.

### Step 4: API reference and skill

1. Add `MaintenanceStats` to `great-docs.yml` beside `CacheStats`; it's exported and the only export missing from the reference.
2. The skill: `MaintenanceStats` in its types list, `maintenance_stats()` in the `Cache`/`FanoutCache` API lists, `Caches.get(name, default=None)` in the `Caches` list, `.maintenance` where it describes `CacheStats`, and "on Unix" for the `0700` directory permissions (Windows inherits the parent's).

### Step 5: benchmark labels

`performance.qmd` and the home-page chart note say the numbers measure "the 0.3.5 code", but `bench/results/macos/gate-3.14.txt` was measured on 60d871b (2026-11-13), before plan 28 merged. Either rerun `bench.gate` on 3.14 and 3.14t on the release commit and update the numbers, or relabel them as "0.3.5 before background maintenance (60d871b)" (see question 1). While there, label or remeasure the 1.18 µs WAL read in `roadmap.qmd` and `performance.qmd`, which doesn't match today's 0.90 µs cached read.

### Step 6: guardrails, so the next release drifts less

These are optional, and cheap enough to keep the audit from having to be repeated in full:

1. A unit test that every name in `diskcache_oxide.__all__` appears in `great-docs.yml`'s reference sections (it would have caught `MaintenanceStats`), with an explicit allowlist for names that deliberately have no entry of their own (`__version__`, say).
2. Run `ruff check` over the Python code cells of every `.qmd` from `scripts/build_docs.py` (or a test), so an undefined name like the missing `Optional` fails the build.
3. A short checklist in CLAUDE.md's Docs convention: when a behavior changes, grep the docs, the skill and the README for the old claim (for this release: "never write", "scheduled", `expire()`), not just the pages for the new feature.

## Decisions so far (2026-11-16)

- **Benchmarks: rerun (Michael).** On the studio, from the 0.3.5 PGO wheels in venvs of their own (`~/dco-bench/`), not under nice. A first run passed but was skewed by load (both libraries down 3-20%, most on 3.14t), so the numbers of record come from a second run once the machine is quiet.
- **When: now (Michael).** Steps 1-4 go ahead while the benchmark waits for a quiet machine.
- **Changelog:** fix the D-060 sentence in place in the 0.3.5 entry. It's a docs error, not a change to what shipped. Michael can override.
- **Commits:** one at the end, per the phase rule.
- **Guardrails: all three (Michael).** The ruff check of the `.qmd` code cells runs in `scripts/build_docs.py` (so `make docs` fails on it), and checks only for real breakage: pyflakes errors such as undefined names, not unused imports or variables, and no style rules.
- **Benchmarks run at normal priority (Michael):** never under `nice`, so they get the whole CPU.

## Questions for Michael

1. **Benchmarks:** rerun the gate on the release commit (on AC power, nothing else heavy running, not under nice) or relabel the existing numbers?
2. **The changelog's D-060 sentence:** fix it in place in the 0.3.5 entry (it's a docs error, and the site's changelog page would show the fix), or leave 0.3.5 as released and note the correction under Unreleased?
3. **When:** do this now, step by step, or wait until the features in flight settle and do it in one pass? Steps 1 and 2 are wrong today and are worth doing now either way.
4. **Guardrails:** which of step 6's three, if any?
5. **Commits:** one commit at the end (the phase rule), or one per step so each can be reviewed on its own?
