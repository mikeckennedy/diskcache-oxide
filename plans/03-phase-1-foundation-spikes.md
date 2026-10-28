# Phase 1: Foundation spikes

Parent: [00-roadmap.md](00-roadmap.md). Follows [02-phase-0.5-conformance-followups.md](02-phase-0.5-conformance-followups.md).

Status: plan reviewed by the architect (round 1 incorporated).

## Goal

Settle the decisions that are expensive to change later, using small, time-boxed spikes measured against the Phase 0 baselines and the conformance suite.

Deliverables:
- `plans/04-design.md`, a written design for Phases 2 and 3, reviewed by the architect;
- a build skeleton that stays.

What happens to spike code:
- Spike 1 and the reviewed core code from Spikes 4 and 5 are kept.
- Spike 2's code is rewritten in Phase 2 around the designed pool and gate.

The roadmap's Phase 1 section and its storage-design notes are the requirements list. This plan orders them into spikes and defines how each one is measured.

## Pinned versions (as of 2026-10-28)

- PyO3 0.30.1.
- rusqlite 0.41.0 with `bundled`. The architect template's 0.40 is stale.
- maturin `>=1.15,<2` in `build-system`.
- A `rust-toolchain.toml` pinning the stable toolchain, with `rust-version` set to PyO3's minimum.
- `uv.lock` committed, and CI uses it.

## Spike 1: build skeleton (kept)

**Layout**
- `Cargo.toml` (workspace, from the architect's template):
  - edition 2024;
  - workspace lints with `unwrap_used = "deny"` and `unsafe_code = "deny"` (not `forbid`; Cargo can't mix `lints.workspace` with per-crate overrides).
- `crates/diskcache-oxide-core`: no PyO3, with `#![forbid(unsafe_code)]` in `lib.rs`.
- `crates/diskcache-oxide-py`: a PyO3 `cdylib` named `diskcache_oxide._core`.
  - Check whether PyO3's generated code passes `unsafe_code = "deny"`. If it doesn't, this crate gets its own copy of the lints table.
  - Expected local allows: `needless_pass_by_value`, `unused_self`, `unreachable_pub`.
- `python/diskcache_oxide/`:
  - a typed `__init__.py` (no `__init__.pyi`, which would shadow the `.py` and drift from it);
  - a hand-written `_core.pyi`;
  - `py.typed`.
- `mypy.stubtest` in CI keeps the stub honest. PyO3's stub generation is still experimental, so it isn't used.

**Build**
- maturin builds with `python-source = "python"`, driven by `uv`.
- `[tool.uv] cache-keys` covers `crates/**/*.rs` and `Cargo.toml`, so `uv sync` rebuilds the extension when Rust changes.
- `panic = "unwind"`, so PyO3 can turn panics into `PanicException`.
- `#[pymodule(gil_used = false)]` from the first commit.

**Errors**
- core: a `thiserror` enum with `Timeout`, `Io`, `Sqlite` (kept as the `#[source]`), `FormatMismatch { found, expected }`, `NotWal { mode }`, `Closed` and `ForkPoisoned`.
- py:
  - `Timeout(TimeoutError)` and `UnsafeValueError(TypeError)`, created with `create_exception!(diskcache_oxide, …)` so they pickle across multiprocessing, and re-exported from `__init__`;
  - `SQLITE_FULL` and I/O errors become `OSError` with an errno;
  - rusqlite's message text never shows up as the exception type.

**SQLite introspection**
- `sqlite_version()` calls the runtime `sqlite3_libversion()`.
- `BUNDLED_SQLITE_VERSION` is the compile-time `SQLITE_VERSION`.
- `sqlite_compile_options()` lists the compile options.
- All three are exposed through `Backend.sqlite_info()`.

**Wheels**
- Per-version wheels for all four Pythons: `cp314`, `cp314t`, `cp315`, `cp315t`.
- 3.14t has no stable ABI, and the free-threaded stable ABI (PEP 803) is 3.15-only and needs module-init support in PyO3, so revisit it at release time.
- Spike 2 benchmarks abi3 against non-abi3 for the GIL builds.

**Checks (local for now; Michael chose no GitHub Actions yet).** `scripts/check.sh` runs everything a CI job would:
- `cargo fmt --check`, `cargo clippy --all-targets -- -D warnings`, `cargo test`, `cargo deny check` (the template's `deny.toml`);
- a release build of the extension into each available environment (3.14 `venv/`, 3.14t `.venv-ft/`, and 3.15/3.15t environments created by the script);
- an extension smoke test and `mypy.stubtest`;
- the conformance suite against diskcache.

`scripts/check.sh --slow` adds the stress tests. CI can be added later from the same script.

**Exit:** `import diskcache_oxide` works on all four Pythons locally, `sqlite_version()` equals `BUNDLED_SQLITE_VERSION`, and `scripts/check.sh` passes.

## Spike 2: latency go/no-go

**Build the path that will ship**
- A release build (`maturin develop --release`) with the real ABI setting.
- `Cache` is the `#[pyclass(frozen)]` itself, with no Python wrapper class. A wrapper adds a Python frame to every call. Decide here whether anything at all needs a Python-level wrapper; `NullCache` and the `CacheLike` Protocol are pure Python and separate.
- A minimal `get`/`set` for `str` and `bytes` values:
  - a small connection pool behind a mutex;
  - cached prepared statements;
  - the planned `key`/`mode`/`value`/`store_seq` columns.
- Stubs for everything else that will sit on the final hot path: the fork gate's shared acquire, the process-generation check, and the native clock read.

**Waiting rules**
- Every blocking wait releases the interpreter: try the lock while attached, and on contention `py.detach` and block. This applies to pool checkout too.
- SQLite reads made while attached use a zero busy timeout; on `SQLITE_BUSY`, retry with the interpreter released.

**Benchmarks**
- Add `get_hit_small_dict` (shaped like the MCP result dict) and `set_small` workloads to `bench/baseline.py`, and re-record diskcache.
- Wire `OxideBackend` far enough for the benchmark to run.

**Method**
- Same session, AC power, 3.14 and 3.14t.
- Interleaved ABAB trials (diskcache, oxide, diskcache, oxide), with a noise tolerance fixed in advance: oxide is "no slower" if its median p50 ≤ diskcache's × 1.05.
- Each run with and without `py.detach` around inline reads.

**Go criteria (all required)**
- `get_hit_small` and `get_miss` p50 no slower than diskcache, on both builds.
- `set_small` p50 no slower than diskcache.
- 8-thread mixed throughput on 3.14t ≥ diskcache's (176k ops/s on this machine).

**Fallbacks, in order**
1. Don't detach for inline reads on GIL builds. Reacquiring the GIL can wait out the 5 ms switch interval, which hurts p99.
2. A connection fast path tied to the calling thread.
3. Check `sqlite_compile_options()` for `DEFAULT_MEMSTATUS`. SQLite's global memory-status mutex limits multi-thread throughput, so compile with `SQLITE_DEFAULT_MEMSTATUS=0`.
4. If it's still short, Michael decides whether parity within some percentage is acceptable. An in-process memo is never an option, because it would break freshness across processes.

**Record:** the cost of `py.detach` on each build, pool-checkout cost with 8 threads on 3.14t, and abi3 vs non-abi3.

## Spike 3: codecs

- **Python-object conversion** lives in the `py` crate: a custom serde visitor that builds Python objects directly, plus an encoder that walks Python objects. Short dict-key strings are cached.
- **Candidates:**
  - JSON: `serde_json` vs `sonic-rs`. sonic-rs relies heavily on SIMD and unsafe code, so note the supply-chain cost if it wins.
  - msgpack: `rmp-serde`, with the standard timestamp extension.
- **Measured round trips** (including Python-object conversion) on Talk Python-shaped values:
  - the 450 KB gzip page-entry dict;
  - the MCP result dict;
  - a rate-limit index list;
  - `asdict(...)` output;
  - a 10 KB nested structure.
- **Compared against** pickle, orjson and msgspec.
- **Tag format:**
  - covers bytes inside containers, datetime with and without tz, tuple, non-str dict keys, NaN/inf, and ints beyond 64 bits;
  - has an escape for user dicts that look like tags;
  - decides the open `value_cases.py` outcomes: `date`, `Decimal`, `set`, and a `ZoneInfo` datetime.
- **Fidelity:** anything that can't round-trip exactly, including subclasses, is rejected with `TypeError`. Pickle is used only when the cache was opened with `pickle=True`.
- **Second latency gate:** `get_hit_small_dict` p50 must be no slower than diskcache's pickle path, using the Spike 2 method.
- **msgpack decision:** the 450 KB page entry decides. Tagged JSON base64-encodes the nested gzipped bytes (+33% on every set). If msgpack clearly wins there, it ships in Phase 2; otherwise it waits for Phase 6a.
- **Tests:** Hypothesis round-trip tests (equal values *and* identical types) in pytest, since the codec lives in the `py` crate.

## Spike 4a: storage experiments

Small Rust prototypes plus pytest runs:

1. **Spilled-value read race (D-016):** the re-check-the-row retry on a missing file, and the compare-and-delete (D-013), run against the Test A race scenario.
2. **SIGKILL mid-spill:** the Test I scenario against the temp-file → rename → commit → delete-old-file protocol.
3. **No writes on open (D-019):** check settings, `user_version` and `journal_mode` first, and write only on a mismatch.
4. **A busy handler that lets writers in:** a short custom poll, plus a pause between maintenance chunks. Target: a single writer with `timeout=1.0` gets in during a 50k-row `clear()` every time.
5. **Prefix scan:** confirm with `EXPLAIN QUERY PLAN` that diskcache's paging query (`WHERE key = ? AND raw = ? OR key > ? ORDER BY key LIMIT 100`) defeats the range scan. That would make each chunk O(n) and the whole scan quadratic, which matches 16× the time for 4× the keys. Then prototype an index-backed range query with a keyset cursor.

## Spike 4b: storage design (written)

Every storage-design bullet in the roadmap, informed by 4a:

- **Schema:**
  - typed key column, `mode`, `store_seq`, expiry and eviction indexes, `size`, and the file reference;
  - `rowid` gives insertion order and `store_seq` gives store order.
- **Persisted settings.**
- **WAL:** required and verified after open; `synchronous`; checkpoint policy.
- **Spill threshold:** sets the adapter's `spill_threshold`.
- **Directory permissions:** 0700 (D-015).
- **Pool:** size, checkout, `transact` pinning, iteration in chunks, waits bounded by `timeout`, and the fd budget read from the cache.
- **Chunked maintenance** bounded by a `store_seq` snapshot.
- **Clock** read before any connection is taken.
- **Format-version mismatch:**
  - During a deploy, old and new workers overlap, so a binary must never rewrite or delete a database in a newer format than it knows. It raises `FormatMismatch`.
  - An older format is upgraded only if a migration exists; otherwise it also raises. Caches are disposable, so there's no automatic wipe; the app decides.

## Spike 5: fork barrier (prototype)

**Registration**
- `os.register_at_fork(before=…, after_in_parent=…, after_in_child=…)`, called from the `#[pymodule]` init, so it's installed even when `_core` is imported directly.
- Not `pthread_atfork`. CPython runs the Python before-hooks *before* the free-threaded stop-the-world; `pthread_atfork` prepare handlers run *after* it, so waiting there for a thread that needs to re-attach deadlocks. It would also need unsafe code.

**Gate.** A small custom gate, not `std::sync::RwLock`:
- an atomic holder count, a fork-pending flag, a `Mutex` + `Condvar`, and a thread-local depth counter;
- shared entry is reentrant (a nested operation inside `transact` must not deadlock behind a pending fork);
- every SQLite call holds the gate in shared mode, including opening and closing connections, which take SQLite's global unix mutex;
- the before-hook takes the gate exclusively with the interpreter released and no short timeout. Forking inside your own transaction is detected by thread identity: it waits until the holder count equals the calling thread's own depth.

**Rust mutexes across fork**
- After taking the gate, the before-hook briefly locks every live pool's inner mutex. A registry of `Weak<Pool>` tracks them.
- It releases them in both after-hooks, so no Rust mutex is copied into the child in a locked state.

**Process generation**
- A `static GEN: AtomicU64`, bumped in `after_in_child` and compared on checkout. This is cheaper than calling `getpid()`, which is a syscall on macOS.
- `PooledConn { conn, gen }`. A pool that sees a stale generation `mem::forget`s its idle connections and resets its in-use count. A stale connection that comes back is forgotten, not counted.
- `mem::forget` is safe Rust, so core stays `forbid(unsafe_code)`. No `ManuallyDrop` fields.

**Forking inside your own transaction.** In the child, the inherited transaction is poisoned: operations inside it and its `__exit__` raise `ForkPoisoned`/`RuntimeError`, and nothing commits. The before-hook may emit a `RuntimeWarning`.

**Test hook.** Provide `Cache._hold_write_transaction()`.

**Exit**
- The Phase 0.5 fork tests pass against the prototype: the deterministic D-012 holder test, the two hammer tests and Test C2, 20 out of 20 runs each on 3.14 and 3.14t.
- No deadlock when forking while threads are checking out connections.
- No stall of a stop-the-world GC on 3.14t while threads wait on the gate.

## Testing strategy

- **Rust unit tests in core:** schema, the spill protocol, pool generations and chunk bounds. Never fork inside `cargo test`; its harness is multi-threaded.
- **Pytest:** fork, process and conformance tests, plus the codec's Hypothesis tests.

## Order

1. Spike 1 (it unblocks everything).
2. Spike 2 (the go/no-go before investing further).
3. Spikes 3 and 4a in parallel; they're independent, so subagents can run them.
4. Spike 4b, then Spike 5, which builds on the pool, the gate stub and the schema.
5. Write `plans/04-design.md`, the architect's review of it, the end-of-phase review, then commit and push.

## Exit criteria

- The build skeleton builds, and `scripts/check.sh` passes on macOS for 3.14, 3.14t, 3.15 and 3.15t. Linux comes when CI is added.
- Both latency gates passed, with the numbers recorded.
- Codec choices and the tag format are decided and benchmarked.
- `plans/04-design.md` covers the storage, concurrency, fork, codec, error and Python-API decisions, and the architect has reviewed it.
- The fork-barrier prototype passes the Phase 0.5 fork tests.
- No change to the diskcache conformance results.

## Out of scope

The full `Cache` API (Phase 2), `NullCache` (Phase 2), Talk Python changes, PyPI releases, and anything in Phase 6.

## Decisions (Michael, 2026-10-28)

- **CI:** local only for now, using `scripts/check.sh`. No GitHub Actions yet.
- **Spike code:** keep Spike 1 and the reviewed core code from Spikes 4 and 5. Spike 2's quick get/set is rewritten in Phase 2.
- **Branching:** stay on `main`. Throwaway spike code lives under `spikes/` and is removed or promoted before the end-of-phase commit and push.

## Results

### Spike 1: build skeleton (done, 2026-10-28)

**Workspace**
- `crates/diskcache-oxide-core` (`#![forbid(unsafe_code)]`, rusqlite 0.41.0 bundled, the `thiserror` error enum).
- `crates/diskcache-oxide-py` (PyO3 0.30.1, `#[pymodule(gil_used = false)]`).
- `python/diskcache_oxide/` (a typed `__init__.py`, a hand-written `_core.pyi`, `py.typed`).
- maturin 1.15 as the build backend, and `rust-toolchain.toml` pinned to 1.99.0.

**Environments.** `venv/` (3.14), `.venv-ft/` (3.14t), `.venv-315/` (3.15), `.venv-315t/` (3.15t). Rebuild any of them with `UV_PROJECT_ENVIRONMENT=<env> uv sync --python <ver>`; the uv `cache-keys` trigger a rebuild when Rust sources change.

**`scripts/check.sh`** passes on all four Pythons:
- fmt, clippy `-D warnings`, cargo test and cargo-deny all clean;
- the smoke test: bundled SQLite 3.54.1 equals the runtime version, the exception hierarchy is right, and `-W error` shows no GIL re-enable warning on the free-threaded builds;
- `mypy.stubtest`;
- the conformance suite: 252 passed, 7 xfailed, on each Python.

**Findings**
- PyO3's macro-generated code passes `unsafe_code = "deny"` and clippy pedantic, so the py crate uses the workspace lints unchanged. `PyO3` was added to clippy's `doc-valid-idents`.
- PyO3 0.30.1 already ships `abi3t`/`abi3t-py315` features (the PEP 803 free-threaded stable ABI). The free-threaded stable ABI is therefore an option for 3.15 now. Revisit at release time; for now we build per-version wheels.
- PyO3 generates `_core.__all__`, which the stub has to list for stubtest.
- `Timeout` and `UnsafeValueError`, created under the `diskcache_oxide` module name, pickle round-trip correctly.
- `--backend=oxide` now imports successfully (the module exists but has no `Cache` yet), so the conformance suite against oxide fails test by test rather than with a `UsageError`. That's expected until Phase 2.

### Spike 2: latency gate (GO, 2026-10-28)

**Setup.** A minimal `Cache` exposed as the `#[pyclass(frozen)]` itself (`crates/*/src/spike.rs`, replaced in Phase 2):
- a pool of up to 8 idle connections behind a `Mutex<Vec<_>>`;
- `prepare_cached` statements;
- the planned `key`/`mode`/`value`/`store_seq` schema;
- stubs on the hot path: an atomic fork-gate holder count, a generation check (stale connections are `mem::forget`-ed), and a native `SystemTime` clock read.

`get` builds the Python object straight from SQLite's row buffer, with no intermediate copy. This decode runs while the connection is held, but it executes no user Python, so it doesn't break the no-Python-while-holding rule.

The gate ran as `bench/gate.py`: 4 interleaved rounds, a fresh process per run, AC power, release build, tolerance 1.05×.

| Workload | diskcache 3.14 | oxide 3.14 | oxide+detach 3.14 | diskcache 3.14t | oxide 3.14t | oxide+detach 3.14t |
|---|---|---|---|---|---|---|
| `get_hit_small` p50 | 1.89 µs | 0.80 µs (0.42×) | 0.84 µs | 1.72 µs | 0.82 µs (0.48×) | 0.84 µs |
| `get_miss` p50 | 1.48 µs | 0.73 µs (0.49×) | 0.75 µs | 1.40 µs | 0.74 µs (0.53×) | 0.76 µs |
| `set_small` p50 | 25.3 µs | 12.9 µs (0.51×) | 13.1 µs | 25.6 µs | 13.4 µs (0.52×) | 15.1 µs |
| 8 threads, mixed | 182k ops/s | 404k (2.21×) | 331k | 183k ops/s | 338k (1.85×) | 325k |

**Verdict.** GO on both builds, with every workload about 2× better. No fallback was needed.

**Findings for the design**
- `py.detach` around SQLite calls costs about 0.03 µs on single calls and 15-20% of multi-thread throughput on the GIL build. The design should **not detach for inline reads and small writes**. It should detach only on contended waits (pool, busy) and file-spill I/O, as the waiting rules already say.
- On 3.14t, 8 threads deliver *less* throughput (338k) than on the GIL build (404k), so free-threading isn't buying parallelism yet.
  - Rebuilding SQLite with `SQLITE_DEFAULT_MEMSTATUS=0` and `SQLITE_THREADSAFE=2` (via `LIBSQLITE3_FLAGS`) helped only a little, 338k → 356k.
  - The likely remaining bottlenecks are SQLite's unix-VFS global mutex around WAL shared-memory locking, and our pool mutex.
  - This is Phase 4 tuning, recorded here. The two SQLite flags are a candidate default for the design (`.cargo/config.toml` `[env]`). Each pooled connection is only ever used by one thread at a time, so `THREADSAFE=2` (multi-thread mode) is safe for us.
- abi3 vs non-abi3 isn't measured yet. Per-version wheels are the plan, so this was deferred to release time.

### Spike 3: codecs (done; full report in [spike-3-codecs.md](spike-3-codecs.md))

- **JSON: serde_json with `float_roundtrip`.** Without that feature, Hypothesis found floats one ULP off. sonic-rs was rejected: 0.5.10 decodes `-0.0` as `+0.0`, and it adds about 30 crates and about 430 `unsafe` sites.
- **msgpack ships in Phase 2, and as the default** (Michael, 2026-10-28). On the 450 KB page entry, a round trip is 11-16 µs with msgpack against 389 µs with tagged JSON, and storage is 461 KB against 614 KB. msgpack also wins on every smaller payload.
- **Tag format specified.** `date`, `Decimal`, `set`/`frozenset` and ZoneInfo datetimes now round-trip, and `value_cases.py` was updated to match. Naive `fold=1` datetimes round-trip too, since `datetime.now()` produces them during the DST fall-back hour.
- **Second latency gate, projected pass.** diskcache's pickle `get` of `mcp_result()` is 2.71 µs on 3.14 and 2.48 µs on 3.14t; oxide is projected at about 1.3 µs.

### Spike 4a: storage experiments (done; full report in [spike-4a-storage.md](spike-4a-storage.md))

All five experiments hit their targets:
- **Spill read race:** 0 misses in 1.98M reads. The plain retry could livelock, so it now falls back to a locked read after 8 optimistic attempts.
- **Compare-and-delete:** 0 live rows deleted, against about 5.5k for a delete-by-key control.
- **SIGKILL:** 600 kills, with `integrity_check` ok every time.
- **Opening without writing:** verified.
- **Writers during `clear()`:** a 100 µs busy poll plus a 100 µs pause between chunks got a waiting writer in 20/20 times, against 0/20 with SQLite's default handler.
- **Prefix scan:** confirmed that diskcache's scan is quadratic (upstream bug #11, 25.6 s at 400k keys); our keyset scan takes 10.2 ms.

### Spike 4b: storage design (done)

Written as [04-design.md](04-design.md), sections 3-7 and 9.

### Spike 5: fork barrier (done)

See 04-design.md section 8.
- All 9 fork tests passed 20/20 on 3.14 and 3.14t. The 50-fork stress run with 8 threads and a GC loop passed with no hangs.
- A deadline-based busy handler fixed a 1-4% first-write starvation in forked children.
- Forking inside your own transaction leaves that database unwritable in the child. That's now fail-fast, recorded as D-020.
- `scripts/check.sh` passes on all four Pythons: 256 passed, 7 xfailed, and 21 cargo tests.
