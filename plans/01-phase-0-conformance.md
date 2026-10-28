# Phase 0: Behavior spec and conformance harness

Parent: [00-roadmap.md](00-roadmap.md)

Status: plan reviewed by the architect (round 1 incorporated; claims spot-checked against real diskcache 5.6.3 on 3.14).

## Goal

A black-box pytest suite that captures the diskcache 5.6.3 behavior we promise to preserve. It runs against real diskcache now and against diskcache-oxide from Phase 2 on, through one backend switch. It also produces baseline performance numbers to measure against later. No Rust in this phase.

## Clean-room rule

Tests are written from the diskcache documentation and from observing its behavior through the public API. We do not copy diskcache source into this repo, and later phases implement from the test suite and the docs, not from diskcache's code. Where we need an internal detail (e.g. the SQLite filename, for lock tests), it goes through the backend adapter, never into a test body.

## Repo scaffolding (step 1)

- Add `venv/`, `.venv*/`, `*.sqlite*`, and `.pytest_cache/` to `.gitignore` (`venv/` is currently not ignored).
- A root `pyproject.toml` with `[tool.uv] package = false` for now (maturin takes over the build in Phase 1), Python `>=3.14`, and a `dev` dependency group: `pytest`, `pytest-timeout`, `diskcache==5.6.3`. pytest-timeout is configured with `timeout_method = "signal"`; the thread method starts a thread, which breaks the fork tests.
- Two uv environments: `venv/` (3.14 GIL; Michael's profile sets `UV_PROJECT_ENVIRONMENT=venv`) and `.venv-ft/` (3.14t, via `UV_PROJECT_ENVIRONMENT=.venv-ft uv sync --python 3.14t`). The suite must be green on both against diskcache.
- Layout:

```
tests/
  conformance/
    conftest.py        # backend option, fixtures, divergence marker + registry check
    backends.py        # adapter: DiskcacheBackend now, OxideBackend stub
    value_cases.py     # fidelity table
    test_basic.py
    test_expiry.py
    test_add_incr.py
    test_eviction.py
    test_locking.py       # timeout, reads-never-write, chunked maintenance
    test_maintenance.py   # clear / expire / volume / len / iteration / keys
    test_values.py
    test_threads.py
    test_processes.py     # multi-process, fork, spill concurrency
  DIVERGENCES.md
bench/
  baseline.py
  results/              # committed JSON baselines
```

## Backend adapter (step 2)

`--backend=diskcache|oxide` (default `diskcache`; also via `DCO_BACKEND`). If the selected backend can't be imported, `pytest_configure` raises `pytest.UsageError` with a clear message, so there's never an ambiguous collection failure.

The adapter exposes:

- `make_cache(directory, *, timeout, size_limit, cull_limit=None, clock=None, **opts)`, which returns an object with the public `Cache` API. It filters options: oxide-only options (`pickle=`, `serializer=`) are dropped for diskcache, and `cull_limit` is dropped for oxide if it isn't supported.
- `db_path(directory)`, which returns the SQLite file (`cache.db` vs `diskcache-oxide.sqlite`).
- `empty_volume(directory)`, the `volume()` of a freshly created empty cache. Eviction thresholds are written relative to it, because the two backends' base volumes differ.
- `spill_files(directory)`, which lists the value files on disk (for the orphan checks).
- `timeout_error`, the exception class.
- `supports(feature)`. A test of a feature planned for a later phase is skipped with the reason `"not yet: <feature>"`. A conftest hook prints a summary count of these skips, so they can't hide real failures.

**Clock fixture.** `fake_clock` starts at the real `time.time()` (not 0) and has `.advance(seconds)`. For diskcache it monkeypatches `time.time`, as Talk Python's tests do (diskcache does `import time` and calls `time.time()`, verified). For oxide it is passed as `clock=`. The fake clock is in-process only, so it does not reach spawned or forked processes, and multi-process tests use real time. Tests that depend on eviction order call `advance()` before each `set`, because a frozen clock gives every entry the same store time and makes the order ambiguous.

**Divergence marker.** `@pytest.mark.diverges(backend="oxide", ref="D-005", reason="...")` applies `pytest.xfail(strict=True)` on the named backend. If the divergence ever goes away, the test starts passing, which strict xfail reports as a failure, and that forces an update. A conftest check at session start fails the run if any `ref` is missing from `DIVERGENCES.md`, or if any `DIVERGENCES.md` entry that is marked "test-visible" has no test referencing it. The divergences the fixtures hide (the clock mechanism and the database filename) are user-facing notes, not markers.

## Test catalog (step 3)

Each test asserts observed diskcache behavior. Surprising behavior gets a comment. Expected values are verified by running against diskcache, not assumed. Items marked ✔ were already confirmed during review.

**test_basic.py**
- `get` of a missing key returns the default (`None`, or the value given).
- `set` returns `True`; a later `get` round-trips the value.
- `delete` returns whether the key existed.
- `__getitem__` on a missing key raises `KeyError`. Also `__setitem__`, `__delitem__`, `__contains__`.
- `pop(key, default)` and `touch(key, expire)` return values.
- Context manager closes the cache; record the behavior after `close()` (diskcache reopens lazily).
- `bool(cache)` is False when the cache is empty (because of `__len__`; Talk Python's wrapper works around this).
- Two `Cache` objects on the same directory see each other's writes immediately.
- `size_limit` and `cull_limit` persist across reopen; `timeout` does not and goes back to 60 ✔.
- `Timeout` subclasses only `Exception` on diskcache ✔. On oxide (D-007) it also subclasses `TimeoutError`.

**test_expiry.py** (all using `fake_clock`)
- `set(expire=N)`: a hit at N−ε, a miss at exactly N ✔. `expire=0` and negative values are misses immediately ✔.
- Expire passed positionally (`set(k, v, 60)`).
- After expiry, `get` misses and `in` is False, while `len`, `iterkeys`, and `volume` still count the entry until `expire()`/culling ✔. `clear()` counts expired rows too ✔.
- `touch` extends or removes the expiry.
- `set(expire=None)` never expires.

**test_add_incr.py**
- `add` on a missing key returns True; on a present key it returns False and leaves the value unchanged.
- `add` over a present-but-expired key returns True ✔ and stores the new value.
- `incr` on a missing key starts from `default=0`; with `default=None` it raises `KeyError`.
- `incr` keeps the existing `expire_time` ✔ (the fixed-window pattern `add(k, 0, expire=w)` then `incr(k)`).
- `incr` on an **expired or missing** key creates a fresh counter with **no expiry** ✔. Preserve this behavior. In Talk Python, a key that disappears between `add` and `incr` (culled, expired, or swept) leaks a key that never expires. That's fixed app-side with `incr(default=None)` and a retry.
- `incr(key, default=None)` on a missing key raises `KeyError` and creates nothing (the app fix depends on this).
- `incr` with a delta, negative deltas, and `decr`.
- `incr` on a float works (`2.5`) ✔. On `True` it raises `TypeError` ✔ (oxide must not turn `True` into 2). On a str it raises `TypeError` ✔.

**test_eviction.py** (thresholds relative to `empty_volume()`; clock advanced on each set)
- Default policy is least-recently-stored: the oldest stored keys go first, and `get` does not refresh an entry.
- Overwriting a key moves it to the back of the eviction order ✔, but not of the `__iter__` order ✔.
- Culling removes expired entries first. Each `set` culls at most `cull_limit` (10) entries ✔, so after one triggering `set` the volume can stay above the limit. With large values the bound is "the limit plus `cull_limit` × the largest value", not a tight bound.
- Under repeated `set`s, the volume converges back below the limit.
- **A `size_limit` below the empty-database volume wipes everything**, including the value just stored: `set` returns True but `len` is 0 ✔. On diskcache, record this behavior. On oxide (D-005), any `size_limit` under 1 MiB raises `ValueError`.

**test_locking.py**
- Timeout: a second raw `sqlite3` connection holds `BEGIN IMMEDIATE` on `db_path()`. Every write op (`set`, `add`, `incr`, `delete`, `clear`, `expire`, `touch`) with `timeout=0.05` raises `timeout_error` within **0.5 s** ✔ (observed about 60 ms). No tight lower bound is asserted.
- **Reads never write:** under `BEGIN EXCLUSIVE` from a second connection, these all succeed: `get` (a hit, an expired entry, and a spilled value), `in`, `len`, `iterkeys`, and `volume` ✔. This proves both WAL mode and that reads don't write. IMMEDIATE would not prove it, because it lets readers in under rollback journaling too.
- After the lock is released, writes succeed again.
- **Chunked maintenance:** process A runs `clear()` (and separately `expire()`) over 50k keys. Process B's `set` with `timeout=1.0` succeeds during that run, which proves the write lock is released between chunks.

**test_maintenance.py**
- `clear()` and `expire()` return counts, and `expire()` removes only expired entries.
- `volume()` grows with large values, which include spill files.
- `__len__` matches the stored entries (including expired ones; see expiry).
- `iterkeys()` is in sorted key order (`'10' < '2' < 'a'`) ✔ and supports `reverse=True` ✔. `__iter__` is in insertion order ✔.
- Keys: the empty string, non-ASCII (the `iterkeys` order must equal Python's `sorted()`), and a very long key (10 KB).
- Prefix scan: `[k for k in iterkeys() if k.startswith(p)]` returns the right keys.
- Large values: 450 KB and 5 MB bytes values round-trip exactly.

**test_values.py** is driven by `value_cases.py`, a table of `(id, value, oxide_default, oxide_pickle)`. Each outcome is one of `roundtrip`, `TypeError`, or `TBD`. Comparisons check `type()` exactly and recursively, not just `==`. On diskcache every case is `roundtrip` (it pickles everything).
- Cases: `str` and `bytes` (empty, large, non-UTF-8 bytes); `int` (0, negative, 2**63, 2**64+1); `float` (`-0.0`, NaN, inf); `bool` (must not come back as int); `None`; nested `dict`/`list`; `tuple`; a dict with int keys; bytes inside a dict (the Talk Python gzip-entry shape); `datetime` (naive and aware); `date`; `Decimal`; `set`; a dict shaped like our tag format; a slots dataclass (the homepage-snapshot shape).
- `date`, `Decimal`, and `set` stay `TBD` until Phase 1 settles the tag format.

**test_threads.py** (meaningful on 3.14t; counts are calibrated early, see Flakiness)
- N threads × M `incr` on one key through **one** shared `Cache` give an exact final total.
- N threads racing `add` on one key: exactly one gets True.
- Mixed `get`/`set`/`delete` from many threads, with no exceptions and no corruption (each value carries a checksum of its own key).
- The same tests again with one `Cache` per thread on the same directory.

**test_processes.py** (always uses an explicit `multiprocessing.get_context(...)`, because the default start method differs: spawn on macOS, forkserver on Linux since 3.14)
- 8 spawned processes start together on a `Barrier` and race `add` on one key: exactly one gets True.
- N processes × M `incr` give an exact total.
- Fork safety: the parent uses the cache, then forks, both with `os.fork()` and with the `fork` context. Child and parent both read and write, and afterwards `PRAGMA integrity_check` is `ok` ✔. These tests run in a subprocess so macOS warnings about forking with threads can't affect the session.
- Worker overlap: process A has the cache open while process B opens it, writes, and exits; A sees B's writes.
- **Spill-file concurrency:** several processes overwrite and delete values of 100 KB and up, while reader processes check the checksums embedded in each value. Requirements: no torn reads (a read is either a miss or a valid value), and once everything is quiet, `spill_files()` matches the rows that are still live, with no orphans.

## Baseline benchmarks (step 4)

`bench/baseline.py` is a plain script that writes JSON to `bench/results/<backend>-<python>-<machine>.json`.

Method:

- Warm up first. Time the p50 numbers in batches (for example, 1,000 operations per `perf_counter_ns` pair), because one timer call costs about 50–100 ns against a 3 µs operation. Collect per-call samples only for p99.
- Run with `gc.disable()` during timing.
- Run at least 5 trials and report the median.
- Record machine, OS, Python build, and power state. Compare only results from the same machine.

Workloads:

- `get` hit with a small str value (about 100 bytes); `get` miss
- `get` of the gzip page-entry dict (450 KB of bytes inside it)
- `set` of that 450 KB entry with `expire`
- `add` followed by `incr` on a rate-limit counter
- `iterkeys` prefix scan at 100k and 400k keys
- `len()` and `volume()` at 400k keys (oxide must not regress these to a `COUNT(*)` scan)
- 8 threads doing mixed `get`/`set` (throughput, on 3.14 and 3.14t)

Run against diskcache on 3.14 and 3.14t, and commit the results. Talk Python's measured numbers (`get` p50 ≈ 3 µs, `set` of 450 KB ≈ 0.45 ms) are a sanity check.

## DIVERGENCES.md (step 5)

Each entry has an ID, the behavior on each backend, the reason, whether it is test-visible, and the tests it affects. Initial entries:

- **D-001** (test-visible): with `pickle=False` (the default), a value our format can't represent raises `TypeError` at `set`.
- **D-002** (note): the database file is `diskcache-oxide.sqlite`, not `cache.db`.
- **D-003** (note): the clock is controlled with the `clock=` parameter; a monkeypatched `time.time` is not followed by default.
- **D-004** (test-visible): keys are `str` only until Phase 6a.
- **D-005** (test-visible, decided): `size_limit` below a fixed minimum of 1 MiB (`MIN_SIZE_LIMIT = 1_048_576`) raises `ValueError` instead of silently wiping the cache.
- **D-006** (note): the empty-database volume differs, so absolute `size_limit` values behave slightly differently.
- **D-007** (test-visible, decided): `diskcache_oxide.Timeout` subclasses `TimeoutError`. Code that catches `Timeout` or `Exception` behaves the same, and `except TimeoutError` now catches it too.

Anything new found during this phase gets added for Michael to review.

## Flakiness controls

- Timing assertions use generous upper bounds only (0.5 s for a 0.05 s timeout).
- pytest-timeout uses the signal method; every multi-process test has a timeout.
- Step 2 includes a calibration run on 3.14t. Counts in the thread and process tests are scaled so the full suite runs in under about 60 s. Anything heavier gets `@pytest.mark.slow` and is excluded by default.

## Steps (one at a time, each a small commit)

1. Scaffolding: `.gitignore`, `pyproject.toml`, uv environments, and an empty suite that runs.
2. Backend adapter, fixtures, the divergence marker and registry check, and a smoke test. Then the calibration run on 3.14t.
3. The test files in this order: basic, expiry, add/incr, eviction, locking, maintenance, values, threads, processes. Run each against diskcache on 3.14 and 3.14t before moving to the next.
4. Baseline benchmark script and the committed results.
5. `DIVERGENCES.md`, with a short write-up of surprising diskcache behavior found along the way.
6. Architect review of the completed phase.

## Exit criteria

- The whole suite is green against diskcache 5.6.3 on 3.14 and 3.14t, in under about 60 s (not counting `slow` tests).
- `--backend=oxide` stops with a clear `UsageError`, which proves the switch works.
- The divergence registry check passes.
- The baseline JSON files are committed.
- Michael has reviewed `DIVERGENCES.md` (including the D-005 and D-007 decisions).

## Out of scope

Rust code, CI workflows (Phase 1 sets them up with the build skeleton), and Phase 6 features (tags, memoize, FanoutCache, and so on). Those get their own conformance tests when their phases begin.

## Results (Phase 0 complete, pending architect review)

**Suite:**
- 210 passed, 54 skipped (oxide-only or not yet supported), and 2 xfailed (the D-008 NaN cases on diskcache).
- About 6.5 s on 3.14 and on 3.14t, stable across repeated runs.
- The one `slow` test (spill-file concurrency stress) takes about 9 s and is green on both builds.
- `--backend=oxide` stops with a `UsageError`.
- The divergence registry check passes.

**Plan corrections found while writing the tests:**
- Not every value round-trips on diskcache: a top-level NaN reads back as `None` (D-008).
- diskcache's chunked `clear()` does commit in 100-row chunks, but a writer waiting on a single `timeout=1.0` call only got in between chunks in about 3 of 10 runs. The test instead asserts the guarantee diskcache does meet: a retrying writer with a short timeout commits at least one write while the call runs. The stronger guarantee moves into the oxide storage design (roadmap, Phase 1).

**Surprising diskcache behavior** (each verified with a probe script):
- A top-level NaN reads back as `None`, ignoring the default (D-008).
- `expire()` stops after 100 rows that share one expire time (D-010).
- `clear()` keeps deleting rows written while it runs, and never finishes against a writer that doesn't stop (D-011).
- `set` sweeps up to `cull_limit` expired entries even with no size pressure. Once the cache is over its limit, one `set` evicts `cull_limit` of the oldest entries, sometimes including the value just stored.
- A `size_limit` below the empty-database volume (about 32 KB) wipes the cache on every `set` (D-005).
- `incr` on an expired or missing key creates a counter with no expiry. Talk Python has since fixed the app side with `incr(default=None)` and a retry.
- `incr` on `True` raises `TypeError`; `incr` past 2**63 raises `OverflowError`.
- `pop` on a missing key returns `None`; `touch` on an expired key returns False.
- A closed cache reconnects silently when it's used again.
- Spill files held up under concurrency: no torn reads and no orphaned files.
- Fork safety held for both `os.fork()` and the multiprocessing fork context.

**Baselines** (`bench/results/`, on battery power):

| Workload | 3.14 | 3.14t |
|---|---|---|
| `get` hit, small (p50 / p99) | 1.96 / 2.67 µs | 1.74 / 2.42 µs |
| `get` miss (p50) | 1.52 µs | 1.44 µs |
| `get` 450 KB page entry (p50) | 30 µs | 30 µs |
| `set` 450 KB page entry (p50 / p99) | 493 / 706 µs | 468 / 614 µs |
| `add` + `incr` (p50) | 21.7 µs | 20.9 µs |
| 8 threads, mixed load | 173k ops/s | 184k ops/s |
| `iterkeys` prefix scan, 100k / 400k keys | 1.7 s / 27.0 s | 1.7 s / 26.8 s |
| `len` / `volume`, 400k keys | ~0 ms | ~0 ms |

On 3.14t diskcache gets almost nothing from free-threading: 8 threads are only 6% faster. That's a clear target for Phase 4.

**Added after the plan was written:**
- Divergences D-008 to D-011.
- The built-in `NullCache` decision. It lands in the roadmap and is built in Phase 2; it isn't part of Phase 0.
