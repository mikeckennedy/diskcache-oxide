# diskcache-oxide roadmap

A clean-room, pure-Rust reimplementation of [python-diskcache](https://grantjenks.com/docs/diskcache/) with the thinnest possible Python layer. This document is the high-level plan; each phase or spike below gets its own plan file in `plans/` before work starts, and the Rust architect reviews each plan and each completed phase.

## Goals

- **diskcache API compatible, not data compatible** (Michael, 2026-10-28). Keep diskcache's API, but not its on-disk format or its patterns: sticking to those would lock in designs we've already shown we can improve dramatically. First the Talk Python subset of `diskcache.Cache`, then near-full API parity for the features we choose to adopt.
- Pure Rust core (SQLite + file storage, eviction, expiry) behind a minimal PyO3 binding.
- Safe on free-threaded CPython (3.14t/3.15t) and GIL builds from the first commit (`gil_used = false`), and safe across processes sharing one directory, including after a fork.
- Native, fast JSON serialization in Rust that never silently changes a value's type, with tagged extensions, an optional msgpack format, and pickle only when the caller opts in.
- Full type hints and `py.typed`.
- Optional async API that cannot slow down the sync path.
- Behavior first, optimization second: every phase keeps the conformance suite green before it chases speed.

## Non-goals (for now)

- Reading existing diskcache directories. We use our own on-disk format; caches start cold on cutover.
- A `diskcache`-named shim module (it would clash with the real package).
- DjangoCache.
- Python versions older than 3.14.

## Key decisions so far

| Topic | Decision |
|---|---|
| Names | Distribution `diskcache-oxide`, import `diskcache_oxide`. |
| Serialization | A per-row `mode` column: raw `str`, raw `bytes`, plain JSON, tagged JSON, msgpack, or pickle. Each cache picks one structured format with `serializer="msgpack"` (the default since Spike 3: about 25x faster than tagged JSON on the 450 KB page entry, and smaller) or `"json"` (serde_json with `float_roundtrip`; human-readable in the database); reads decode by row mode, so the cache stays readable if the setting changes. The encoder detects anything its format would change (tuples, non-str dict keys, NaN/inf, ints wider than 64 bits, nested bytes and datetime for JSON) and tags it; it never coerces. Tags have an escape mechanism so user data can't be mistaken for a tag. **Pickle is off by default**: a value that can't be represented raises `TypeError` at `set`. `pickle=True` enables pickle as a fallback for that cache. |
| On-disk format | Our own schema in `diskcache-oxide.sqlite` (deliberately not diskcache's `cache.db`, so the two never collide), always in WAL mode (`journal_mode=WAL`, applied on every open). Stamped with a format version (`PRAGMA user_version`); the behavior on a version mismatch is an explicit code path, decided in Phase 1. |
| Keys | `str` only in Phase 2 (`TypeError` otherwise), but stored in a type-tagged column so other key types can be added later without a migration. |
| Clock | Constructor parameter `clock: Callable[[], float] \| None = None`. The default reads the clock natively in Rust at zero cost. Tests pass `clock=lambda: time.time()` to follow a monkeypatched `time.time`. |
| Crate layout | `core` crate with no PyO3 that deals only in `(mode, bytes \| file path)`; `py` crate owns all conversion between Python objects and bytes, including JSON. |
| Wheels | Per the [abi3t migration guide](https://docs.python.org/3/howto/abi3t-migration.html): from 3.15, a single `abi3t` wheel (tag `cp315-abi3.abi3t`) covers both GIL and free-threaded builds, and PyO3 0.30.1 already ships `abi3t-py315`. 3.14 still needs version-specific `cp314` and `cp314t` wheels. The stable ABI may be slower and excludes `PyUnstable_*` APIs (such as deferred refcounting), so we ship **fast per-version wheels for the "tier 1" Pythons, plus one `abi3t` wheel as the catch-all for every future version**. Once 3.14 ages out, the build matrix is small and stable. |
| Platforms | macOS and Linux first: development on macOS, deployment on Linux (Talk Python runs the latest Ubuntu LTS, tested locally in OrbStack Docker). **Windows will be supported** (Michael, 2026-10-28), mainly as a development environment, after macOS and Linux are solid. The Unix-only pieces (the fork gate, `MetadataExt` inode ids) need Windows equivalents or no-ops then. |
| NullCache | A built-in `NullCache` in the Python layer, so a cache can be switched off without null checks in the app. Its rule: **it behaves like a real cache in which every entry expires the instant it is written**. `get`/`pop` return the default, `set`/`add` return True, `incr` returns `default + delta` (and raises `KeyError` when `default=None`), `delete`/`touch`/`in` are False, and `len`/`volume`/`clear`/`expire` are 0. Optionally, `NullCache(invalidate=real_cache)` forwards `delete`, `clear`, `iterkeys` and `pop` (as a delete) to a real cache, so invalidations still land while caching is off and nothing stale is served when it comes back on. More precisely, the rule is "instant expiry plus an immediate sweep": on a real cache, an expired row still counts in `len`/`iterkeys` until it's swept, while a NullCache reports 0 and nothing. `invalidate=` deliberately breaks the model for `iterkeys`, which returns the real cache's keys. The constructor accepts and ignores `Cache`'s arguments, so swapping is a one-line change. A shared `CacheLike` Protocol covers both classes, and a test checks that the two have the same public signatures. |
| Truthiness | `bool(Cache)` is always True (D-009, fixing diskcache's empty-cache-is-falsy gotcha). `bool(NullCache)` is False. Both have an `enabled` property. |
| Async | A separate `AsyncCache`; it starts as a Python executor wrapper and moves into Rust only if measurements justify it. |
| Network | Possible late phase: an HTTP + msgspec server, "Redis replacement backed by SQLite". |
| Reference behavior | diskcache 5.6.3 is the baseline. The [diskcache-community](https://github.com/secondwindoss/diskcache-community) fork (5.8.x, actively maintained, `import diskcache_community`) keeps 5.6.3's semantics for everything in our suite, and serves as a second witness: it can run as an optional third conformance backend. We match the fork where it fixed real bugs: fork safety (its PR #13, our D-012), closing connections left behind by exited threads, and opening a cache never writes when nothing changed. Its `.pyi` stubs are a cross-check for our signatures; we compare against them, never copy them. The bugs we found in diskcache are logged with repros in `upstream/`. |

## What Talk Python actually needs (from the usage survey)

- Only `diskcache.Cache` and `diskcache.Timeout`. String keys only.
- Constructor: `directory`, `timeout`, `size_limit`. Default eviction (least-recently-stored) keeps `get` free of writes, and that property is load-bearing.
- Methods: `get`, `set(expire=)`, `add(expire=)`, `delete`, `incr`, `expire()`, `clear()` (returns count), `volume()`, `__len__`, `iterkeys()`, `close()`, context manager, plus `__iter__`/`__getitem__` in scripts.
- Semantics that matter: `add` is atomic across processes and succeeds over an expired key; `incr` keeps the existing TTL; `Timeout` is raised when the busy timeout runs out; `clear`/`expire` run in chunks so they don't hold the write lock for long; `set` culls when over the size limit.
- Several processes share one directory (worker overlap, scripts, process pools that may fork).
- Calls are made synchronously on the asyncio event loop, so `get` latency must stay in the low microseconds (current p50 ≈ 3 µs; `set` of a 450 KB value ≈ 0.45 ms).
- Values include gzipped `bytes` inside dicts, `datetime`, and slots dataclasses.
- Tests monkeypatch `time.time` to advance the cache clock (solved by the `clock=` parameter).
- Known pain point: `iterkeys()` is slow at 100k+ keys.

## Phases

### Phase 0: Behavior spec and conformance harness

A black-box pytest suite that runs against real `diskcache==5.6.3` through a backend fixture, and later against diskcache-oxide. Coverage:

- expiry, including `add` over an expired key and `incr` keeping the TTL;
- size-limit accounting and culling batch behavior (`cull_limit`);
- `Timeout`;
- `clear`/`expire` return values;
- iteration order;
- multi-process `add` atomicity;
- fork safety;
- value fidelity: tuples, int dict keys, NaN/inf, big ints, nested bytes, datetime, a dict that looks like a tag.

It also records baseline numbers for diskcache: `get` p50, `set` of a 450 KB value, and `iterkeys` at 100k keys.

Deliverable: green against diskcache, with a written list of behaviors we intentionally diverge on.

### Phase 0.5: Conformance follow-ups (before the oxide backend is wired in)

Tests prompted by the diskcache-community fork and upstream issues, each verified against diskcache first:

1. A reader racing an overwrite of a spilled value gets the old or the new value, never the default, `None`, or an error (upstream #258, #294).
2. Many threads and processes open a brand-new directory at the same moment (upstream #266, #349).
3. Two `Cache` instances on one directory, then a fork; both work in the child and in the parent (upstream #325).
4. Many short-lived threads, then `close()`: no file-descriptor growth and no `ResourceWarning` (upstream #295, #336).
5. Reopening and reading leaves the database file unchanged (its hash and mtime).
6. A writer process killed by SIGKILL in the middle of spill writes leaves a consistent cache.
7. A fork after the parent has used SQLite, on macOS. Our bundled SQLite must not hit the Apple system-libsqlite3 crash that the fork documents.
8. Optional: a `community` backend in the adapter, so the suite also runs against diskcache-community.

### Phase 1: Foundation spikes (time-boxed, throwaway allowed)

1. **Build skeleton**: Cargo workspace (`core` + `py`), maturin, and uv; `#[pymodule(gil_used = false)]`; CI that builds 3.14, 3.15, 3.14t, and 3.15t from day one. Check whether the free-threaded stable ABI works with PyO3/maturin. Expose `diskcache_oxide.sqlite_version()`, `sqlite_compile_options()` and `BUNDLED_SQLITE_VERSION` (Test G in Phase 0.5 checks them, so the wheel can never quietly link the system SQLite). Settle the names and route them through a `Backend.sqlite_info()` adapter method.
2. **Latency spike (go/no-go)**: a throwaway end-to-end `get` (PyO3 call, cached-statement SQLite SELECT, small-value decode) whose p50 must be no slower than diskcache's `get` measured on the same machine in the same session (the Phase 0 AC-power baselines are 1.89 µs on 3.14 and 1.76 µs on 3.14t). The bar is relative, never an absolute number, on both the GIL and free-threaded builds. Measure what releasing the GIL / `py.detach` costs. The likely outcome is to release only for file-spill reads.
3. **Codecs (JSON + msgpack)**: a custom serde visitor in the `py` crate that builds Python objects directly, with dict-key string caching, shared by both formats. Benchmark serde_json against sonic-rs for JSON, and rmp-serde for msgpack (with the timestamp extension for datetime), measuring the conversion rather than parse speed. Compare against pickle, orjson, and msgspec, using Talk Python-shaped values (including gzipped bytes inside dicts). Settle the tag format and its escape. Decide whether msgpack ships in Phase 2 or in Phase 6a.
4. **Storage design**:
   - Connection strategy: a small per-`Cache` pool of `rusqlite::Connection`s behind an uncontended `Mutex` checkout, tagged with the pid so connections reopen after a fork.
   - Settings: WAL is mandatory (verify after open that `journal_mode` really is WAL; fail loudly if not, e.g. on network filesystems), plus `synchronous`, the busy timeout, and checkpoint behavior.
   - Schema: mode, typed key, and expiry/eviction indexes, plus a fast key-prefix scan to fix the `iterkeys` problem. Keep two orderings apart:
     - `rowid` is insertion order, which `__iter__` uses. An overwrite keeps its rowid, as pinned by the Phase 0 tests.
     - `store_seq` is a monotonic sequence assigned on every set. Least-recently-stored eviction uses it, and so does the snapshot bound for `clear()`/`expire()` (D-011). A key overwritten during a clear gets a new `store_seq`, so it survives.
   - Read-side pickle policy (D-001, decided): when a `pickle=False` handle reads a pickle row written by a `pickle=True` handle, `get` raises `UnsafeValueError` (a `TypeError` subclass) and leaves the row in place. It never unpickles, and it never destroys a peer's data.
   - Spill threshold: pick oxide's value. The conformance adapter's `spill_threshold` must match it.
   - Investigate why diskcache's prefix scan goes from 1.7 s at 100k keys to 27 s at 400k (16× for 4× the keys) before treating 400k as a target.
   - File spills: inline vs spill threshold. Write to a temp file and rename it, then commit the row; delete the file after the row is removed; a missing file on read is a miss; no fsync (same as diskcache, and documented).
   - Format version stamp and the mismatch policy.
   - Fork barrier (D-012): a fork must never happen in the middle of a transaction. Every transaction holds a process-wide lock in shared mode; an `os.register_at_fork(before=...)` hook takes it exclusively, which waits for in-flight transactions and blocks new ones, and the `after_in_parent`/`after_in_child` hooks release it. The child then drops its inherited connections without closing them (to avoid SQLite's close-after-fork hazards) and opens new ones. Prove this out in the spike against the Phase 0 fork tests, including the case where fork is called from a thread that is itself inside a transaction (that must not deadlock).
   - Spilled-value reads (D-013, D-016): read the row, then open its file. If the file is missing, read the row again; if the file reference or `store_seq` has changed, the value was overwritten, so retry. It's a true miss only when the row still points at the same missing file. The D-013 opportunistic delete is a compare-and-delete (`WHERE rowid = ? AND store_seq = ?`), so a reader can never delete a live row that a writer just replaced.
   - No Python code runs while a transaction or a pooled connection is held. That includes the `clock=` callable, pickling, and JSON conversion of Python objects. Call the clock and serialize before `BEGIN`, and convert to Python objects after the connection goes back to the pool. Otherwise a clock or `__reduce__` that re-enters the cache can deadlock a pool of size 1.
   - Forking inside your own transaction: an exception from an `os.register_at_fork` before-hook is ignored by CPython and the fork goes ahead (verified on 3.14.10 and 3.14t), so diskcache-oxide can't refuse the fork. The barrier waits only for *other* threads' transactions. In the child, the inherited transaction is poisoned: operations inside it and its `__exit__` raise `RuntimeError`, and nothing commits. The cache works on fresh connections outside it. The before-hook may emit a `RuntimeWarning`.
   - Connection pool rules (D-017), reconciled with the no-Python rule above:
     - `transact()` pins one connection to the calling thread for the whole block and counts against the pool. It's the one sanctioned case where user Python runs while a connection is held.
     - Iteration (`iterkeys`, `__iter__`) fetches in chunks and returns the connection to the pool between yields, so no cursor or read transaction is held across user code.
     - Waiting for a free pooled connection is bounded by the cache's `timeout` and raises `Timeout`.
     - The fd budget depends on the pool size (and on any separate writer connection or directory fds kept open for `openat`). The conformance adapter reads it from the cache rather than assuming a constant.
   - Lock order with the fork barrier: take the pooled connection first, then the barrier in shared mode, and never block on the pool while holding the shared barrier. Otherwise thread A (holding the barrier in shared mode, waiting for B's connection), thread B (waiting behind the pending exclusive request) and the fork (waiting for A) deadlock.
   - The before-fork hook waits with the GIL released (`py.detach`). On GIL builds, the thread holding the transaction needs the GIL to finish and commit. The wait has no short timeout. Forking inside your own transaction is detected by thread identity, not by a timeout running out.
   - Child-side fds: the child drops its inherited connections without closing them, so each fork leaks up to the fd budget in the child, by design. Tests that combine forks and fd counts must allow for this rather than use the parent's budget.
   - Opening never writes when nothing changed (D-019): no unconditional `user_version`, `journal_mode` or settings writes on open (check them first, write only on mismatch), and no `PRAGMA optimize` on close.
   - Test hook: until `transact()` lands (Phase 6b), the core exposes a private `Cache._hold_write_transaction()` context manager, so the deterministic D-012 fork test runs against the Phase 1 barrier spike.
   - Maintenance fairness: `clear()`/`expire()`/culling run in chunks bounded by a snapshot taken at the start (D-010, D-011). Between chunks they actually let a waiting writer in. SQLite's busy handler polls rather than queueing, so diskcache's writers often wait out the whole `clear()`. The target is that a writer with `timeout=1.0` gets in during a 50k-row `clear()`, on every run.
   - Leave room for encryption at rest (speculative Phase 8): all connections open through one function, so a key pragma can be applied there; spill-file reads and writes go through a single interface, so an encrypting implementation can plug in. Nothing about encryption is built yet.

Exit: a written design for Phases 2–3, reviewed by the architect.

### Phase 2: Core `Cache` (Talk Python subset, thread- and process-safe)

Implement the Talk Python subset with the decided storage and serialization:

- constructor: `directory`, `timeout`, `size_limit`, `clock`, `serializer`, `pickle=False`, with eviction defaulting to least-recently-stored;
- `get`/`set`/`add`/`delete`/`incr`/`decr`/`touch`/`pop`;
- `expire`/`clear`/`cull`/`volume`/`__len__`/`__contains__`/`__getitem__`/`__setitem__`/`__delitem__`/`iterkeys`/`__iter__`;
- `close`, context manager, and `Timeout`;
- `NullCache` (with optional `invalidate=` forwarding), the `CacheLike` Protocol, and the `enabled` property and truthiness rules.

Thread safety (the pyclass is `Sync`), multi-process stress tests, and fork safety are required in this phase, not deferred. The Python layer is a thin wrapper plus `.pyi` stubs and `py.typed`. The Phase 0 suite passes except for documented divergences. Benchmarks against the Phase 0 baselines.

### Phase 2.5: Concurrency deep review and soak (before any production use)

A separate phase, because the concurrency core is where serious bugs would hide, and so far it has been written and reviewed mostly by agents.
- **Independent deep reviews of the concurrency core** (`gate.rs`, `pool.rs`, the write lock, the `Store` registry, lock waits, the spill protocol) by several coding agents: Claude, Fable and ChatGPT's Astra, run by Michael. Each gets the code and the design, not each other's conclusions. Findings are merged, triaged and fixed.
- **Model-checker tests** (`loom` or `shuttle`) for the gate and write-lock state machines. Feasible because core is pure Rust.
- **Day-long soak runs on Michael's 18-core Mac Studio**, written by Michael with help: many processes and threads at 99:1 and 1,000:1, forks, SIGKILLs, `clear()`/`expire()` under load. Monitor memory (RSS, fds), `integrity_check`, totals vs `COUNT`/`SUM`, checksummed values (no corruption, no lost acknowledged writes), latency drift and orphan files.
- **The same soak, shorter, on Linux** in OrbStack (the latest Ubuntu LTS).
- **Carried in from the architect's Phase 2 review** (details, file:line and fixes are in the Progress log of `05-phase-2-core-cache.md`). Send the independent reviewers to items 1, 2 and 7 first.
  1. The write lock's `waiters`/`starving` counts leak if a wait unwinds (a stuck `starving` would make every `try_lock` fail); use RAII guards.
  2. The blocker re-attaches the interpreter on threads that are already detached (`clear`, `expire`, `cull`, `Store::open`, `begin_hold`). On a GIL build that can cost up to the 5 ms switch interval per chunk. Measure first, then add a "this thread is already detached" flag.
  3. Non-starving waiters keep polling while the lock is reserved for a starving one: wait on `state() != 0 || starving > 0`.
  4. At gate depth > 0, take the lock even when someone is starving (a barging `take()`), so a nested write doesn't race a starving one inside SQLite.
  5. `cull()`'s chunk loop has no yield or priority re-take; route it through `chunked`'s pause.
  6. `begin_hold` waits for the write lock at gate depth > 0 (rule 1's deadlock shape, bounded by `timeout`); use `try_lock` or refuse there before `transact()` makes it public.
  7. `pop` decodes Python inside its write transaction. A pickle `__setstate__` that writes the same cache waits out the full timeout behind its own transaction. Copy the bytes, and decode after `COMMIT`.
  8. Close-then-reuse and a close/open race can leave two `Store`s for one file: decide retirement under the registry mutex, and look up or revive on rejoin.
  9. The pool's `max_idle` stays at the first handle's `max_connections`; raise it on `join`, as `cache_pages` is.
  10. **Built-in WAL management (found by the soak, 2026-10-28; Michael).** With readers in other processes, a burst of writes can't wrap the WAL, so the file grows and never shrinks: 538 MiB-1.1 GiB in a 3.14t reproduction. Done as Phase 2.5 step 1: `wal_size_limit` (default 64 MiB), D-024. Design and results are in `plans/06-phase-2.5-concurrency-review.md`. The forced checkpoint moves to the background maintenance thread when that lands.
  - Plus the cheap first step towards real parking: spin about 2-5 µs, then `yield_now`, before each 50 µs sleep. Real parking needs `unsafe` (a futex or `os_sync_wait_on_address`) in a small separate crate, since core forbids `unsafe`.
- Exit: all review findings resolved or explicitly accepted, and a clean 24-hour soak on macOS plus a clean multi-hour soak on Linux.

### Phase 3: Talk Python adoption

Swap diskcache-oxide in behind the existing `DurableCache`/`cache_service` factory. Gzipped bytes and datetimes go through tags (or msgpack for the page caches). The two dataclass values (homepage snapshot, `list[FeaturedCourse]`) start in a cache that opts into `pickle=True`; converting them to plain dicts and dropping pickle is a follow-up. Switch the clock-patching tests to `clock=` and retire the hand-written `typings/diskcache` stubs. Replace the app's `NullCache`/`PerfTestNullCache` with the library's `NullCache` (`invalidate=` for the perf-test case), and drop `DurableCache`'s `__bool__` workaround. Talk Python's construction-time logging and Sentry report stay in the app's factory. Review the call sites whose behavior changes under the instant-expiry rule. The important one is `add`: with the cache off it now returns True, so scheduled-job claims succeed and jobs keep running, where today they would silently stop. With the cache off, every worker's claim succeeds, so a job runs once per worker. That's fine for idempotent jobs but wrong for jobs with side effects, such as sending email. Claims should probably move to a cache that can't be nulled, or to the Phase 6e Lock recipe. Cutover is a single swap: all caches are cleared at deploy, so no diskcache data is ever read or migrated. Because we use `diskcache-oxide.sqlite`, an old `cache.db` is never opened; the cutover deletes the old diskcache files (`cache.db*` and spill files) to reclaim the disk space. Watch production latency against the old numbers.

### Phase 4: Free-threaded performance

Throughput and contention tuning on 3.14t/3.15t: many threads and processes on one directory, pool sizing, and hot-path profiling (`get` especially). Add a benchmark suite to CI that compares against diskcache on GIL and free-threaded builds.


Expectation (Michael): at a 1,000:1 read ratio, multiple threads should clearly help. Not getting a benefit means something is serializing that we can profile and remove. Ideas to try:
- thread-affine connections (one per thread);
- immutable, frozen shared data read without locks;
- deferred refcounting on `Cache` instances (only in the per-version wheels, since it's a `PyUnstable_*` API);
- removing per-call shared-object traffic in the py layer;
- per-file write lock and `-shm` tuning;
- ~~SQLite's global memory statistics~~ (done in Phase 2): the shipped build sets `SQLITE_DEFAULT_MEMSTATUS=0`, which measured 2-10% more threaded throughput. Memory is measured in a separate instrumented build (`.venv-mem`).

Profile on 3.14t with the share-nothing benchmark before changing anything. **First experiment:** 8 threads on one directory, against 8 threads each with its own `Cache` on its own directory, against 8 directories behind one shared object. That shows whether the wall is per database file (so FanoutCache, 6c, fixes it), process-wide in SQLite, or Python-side contention on the shared object. **First lead (Phase 2 profiling):** about 60% of a cached `get` is the `fcntl` system call SQLite makes for the WAL read lock on the `-shm` file, under a process-wide mutex. Cutting that (a per-process lock-caching VFS shim, or longer-lived read snapshots) is likely both the biggest single-call win and the fix for free-threaded scaling. See 04-design.md section 7.

Findings that seed Phase 4 (2026-10-28, 04-design.md section 7):
- On 3.14t, calls through shared extension objects don't scale (a no-op `_core` call is 3× slower at 8 threads, most likely because of contended reference counts).
- The shared `-shm` file is a secondary limit.
- Same-process writers race on SQLite's write lock.
- Phase 2 fixes the benchmark loop and adds an in-process write lock per database file. Phase 4 tries deferred reference counting on `Cache` instances.

### Phase 5: `AsyncCache`

Start with a Python wrapper that runs calls in an executor; it can't touch the sync path by construction. Move it to a Rust-side pool only if benchmarks justify the change. The docs will say plainly that async `get` of small values is slower than the sync call, and that async mainly helps with large values.

### Phase 6+: Parity, adoption-driven (several phases)

Each item gets conformance tests against diskcache first. Candidate groups, each roughly a phase:

- **6a. Cache options:** eviction policies (LRU/LFU/none), tags and `evict(tag)`, `statistics`/`stats`, `memoize` (+ `__cache_key__`), `peek`/`pull`/`push`, `read=True`, `check()` (including orphan cleanup), `retry=`, `expire_time=`, and non-`str` keys.
- **6b. `transact`:** nested transactions with Python callbacks while a connection is held. Flagged as hard.
- **6c. FanoutCache.** The answer for anything beyond the single-directory upper bound measured in the Phase 2.5 soak: about 180k cache operations a second with about 1,000 writes a second (half of what a Mac mini can do flat out). Include a fanout benchmark: sharding spreads writers over several write locks, which is where oxide's design should shine (Michael expects a big win). Use the standard 99:1 mix at 1-8 threads and several processes, against diskcache's FanoutCache. Candidate for pulling into Phase 4 as the threaded default (Michael, 2026-10-28), if the Phase 4 scaling experiment shows per-file sharding helps. Either way it's being built, so it's worth trying. Decisions (confirmed by Michael, 2026-10-28):
  - **Shard count is a persisted setting of the directory:** fixed by whoever creates it (default 8), read by every later opener. It's never derived from the thread count: every process must route a key to the same shard, or `add()` and `incr()` lose their cross-process atomicity. Changing it means a new directory.
  - **Shard choice:** a fast, fixed-seed hash of the whole key's bytes (not a prefix: real keys share long prefixes), frozen as part of the on-disk format.
  - **`Cache` stays single-file.** FanoutCache is the recommended choice for threaded apps, with its known trade-offs documented: `transact()` within one shard, per-shard culling, and `len`/`clear` fanned out.
  - **Ordering (Michael):** `__iter__` yields every key, shard by shard, with no insertion-order guarantee. That will need its own divergence entry when FanoutCache lands. Sorted `iterkeys(prefix=, reverse=)` stays a cheap k-way merge of the shards' already-sorted keyset pages. `in`/`get` go straight to the key's shard, so they never need to look anywhere else.
  - **One connection budget for the whole FanoutCache**, not `max_connections` per shard, so the memory bound (D-023) still holds.
- **6d. Deque and Index.**
- **6e. Recipes:** Lock, RLock, BoundedSemaphore, throttle, barrier, memoize_stampede.

### Phase 7: Network server (planned; Michael is increasingly keen)

**Storage mode for the server (measured 2026-10-28).** A server process that owns its cache files can run SQLite with `locking_mode=EXCLUSIVE`, which keeps the WAL index in heap memory instead of the shared `-shm` file. That removes the per-read `fcntl` system calls that dominate a multi-process read, and the shared-memory serialization behind poor free-threaded scaling. Measured with Python's sqlite3 on this machine:

| Mode | Read | Write | Multi-process? |
|---|---|---|---|
| WAL (the library) | 1.18 µs | 13.9 µs | yes |
| WAL + EXCLUSIVE locking | 0.69 µs | 16.5 µs | one process only |
| Rollback journal (DELETE / TRUNCATE) | 3.1-3.3 µs | 152-224 µs | yes, but readers block on commit |

The library stays on WAL, since it must let several processes share a directory. The server uses exclusive locking, as the database's only owner.

An HTTP + msgspec service that serves a cache directory to many clients, giving a Redis-like central cache with SQLite underneath. Scope to be decided when we get here.

### Phase 8 (speculative): Encryption at rest

Off by default, and only if a real need appears. SQLCipher for the database and its WAL file; our own AEAD encryption (ChaCha20-Poly1305 or AES-GCM) for spill files, using a key derived from the same secret. Takes a raw 256-bit key, or derives one once and caches it, so pooled and post-fork reconnects stay cheap. Packaging is still open: always bundle SQLCipher, or ship a separate encrypted distribution. Write down the threat model before building it.

### Cross-cutting, every phase

- CI: Linux and macOS, GIL and free-threaded builds, clippy, rustfmt, cargo-deny, mypy/pyright on the stubs.
- Release wheels via maturin-action once Phase 3 is in production.
- Architect review of each phase plan before work starts and of the result when the phase ends.

## Open questions

Ideas that aren't ready to plan live in [vision.md](vision.md).


None blocking right now. Questions for the individual phases are resolved in each phase's plan.
