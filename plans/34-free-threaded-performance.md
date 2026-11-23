# Plan 34: free-threaded performance (roadmap Phase 4)

Status: draft (2026-11-22), reviewed once by the-rust-architect (ready with changes, all folded in; see the Log). Starts after plan 33's spike reports, because the largest known cost on a threaded read is the one that spike studies. Michael's decisions (2026-11-22): the benchmark suite the roadmap put "in CI" becomes an extension of the local `bench.gate`, run by hand on his machines; there is no GitHub workflow.

## The goal

Michael's expectation, from the roadmap: at a 1,000 : 1 read ratio, more threads on a free-threaded build should clearly help, and where they don't, something is serializing that can be found and removed. This plan measures where 0.5.8 stands, removes what it can, and puts the result behind the gate so it can't quietly regress.

## Where things stand

Plan 12 already did the first round (2026-11-05, four threads on one shared handle, 3.14t):

- our own SQLite mutexes with a bounded spin: a plain `Cache` went from 816k to 1,416k reads a second on the Mac and from 1,110k to 1,690k in the Linux VM;
- a spinning slot wait and two connections per shard for `FanoutCache`;
- `SQLITE_DEFAULT_MEMSTATUS=0` in the shipped build.

What that round found and left:

- SQLite's per-file shared-memory mutex (`unixShmLock`) and the two `fcntl` calls under it were 93% of the mutex waits on a shared plain `Cache`. The spin made waiting cheaper; it didn't remove the lock.
- oxide's own share was the pool mutex, taken twice per `get` (7% of waits).
- The gate and slot atomics, PyO3 argument extraction, reference counts and `HandleConfig` were each under 1% of self time at that ceiling. They may matter once the ceiling above them is gone.
- Two known regressions shipped: a plain `Cache` oversubscribed at 36 threads (7% slower on the Mac), and `FanoutCache` at 32 connections with 8 threads (5 to 12% slower, Mac only).
- An older finding (2026-10-28) that a no-op call into `_core` got three times slower at 8 threads has not been measured since. Part of its likely cause is already gone: the pyclasses are frozen, and PyO3 0.30.1's `&self` receiver takes no reference on the object. What's left would be on the interpreter's side of the call.
- `bench.gate` already judges the 99 : 1 and 1,000 : 1 mixes at 1, 2, 4 and 8 threads against diskcache.

All of these numbers are a release or more old. 0.3.5 added the maintenance thread, 0.4.6 the Django backend and 0.5.8 the fork checks on every call. Nothing below is decided until step 1 says where the time goes now.

## Steps

**Step 1: the baseline, on 0.5.8.** One matrix, on the Mac and on htz, with PGO wheels:

- interpreters 3.14t and 3.15t, with 3.14 beside them as the GIL reference;
- 1, 2, 4 and 8 threads in the share-nothing loop;
- the 99 : 1 headline mix and the 1,000 : 1 Talk Python profile, plus reads only;
- a plain `Cache` and `FanoutCache(8)`, one shared handle against a handle per thread;
- controls: a directory per thread (the ceiling, and the yardstick for the targets), a no-op call into `_core`, and diskcache 5.6.3 on the same rows.

`bench/scaling/scale_probe.py` and `m2_probe.py` cover most of this; the step is to run them as one table and keep it in `bench/results/`. The step ends with the targets below confirmed or changed by Michael against real numbers.

**Step 2: the profile.** `sample` on the Mac at 4 and 8 threads and `perf` on htz at 2 and 3 (it has 4 CPUs, so 4 is saturated and 8 oversubscribed), for the plain `Cache` and the `FanoutCache` rows, split three ways: SQLite (the shared-memory mutex and its `fcntl` calls), oxide's core (pool, gate, slots), and the Python boundary (reference counts on shared objects, argument parsing, the codec). The split decides which of the experiments below run and in what order. One that the profile doesn't support is dropped and the Log says why.

**Step 3: experiments.** Each starts as a prototype behind an environment variable in a scratch branch, measured on both machines with interleaved rounds, as plan 12's were. One that wins becomes its own reviewed step with its own tier; one that doesn't is kept as a `.diff` in `bench/results/` with its numbers.

- **3a. Whatever plan 33 recommends.** Keeping the read-mark lock or the read snapshot between reads would take lock traffic out of an unchanged read. In threads the gain will be smaller than in processes: readers that overlap inside one process already skip the `fcntl` calls through SQLite's own counting, so what's left to remove there is mostly the shared-memory mutex. If plan 33 says build it, it's built here as the first experiment and everything after is measured on top of it. If plan 33 says no, this item is closed with its reason.
- **3b. Thread-affine connections** (plan 11, option 2). Each thread keeps the connection it last used for a store and falls back to the pool. It removes both pool-mutex round trips and keeps a connection's page cache on one core. The risk is the fork protocol: a thread-local connection must still carry its generation, be counted in the slots, be reachable by the before-fork hook's quiescence and by `close()`, and be released when its thread exits. That needs new shuttle models before any soak. The review's additions to the risk list: the connection must be closed before a fork, not just quiesced, and counted by `poison_inherited` (`pool.rs`); D-017's bound on connections "however many threads are alive" and D-018's close-from-any-thread rule out a real `thread_local!` with a destructor, so the design is affinity slots inside `PoolShared`; and the retire double-check in `store.rs` with its shuttle mirror, the `Applied` settings and `check_usable` all have to hold on the new path. It may also not clear this plan's own 15% bar: a read would still pay the slot and gate atomics, and plan 12's pool-spin prototype gave 12% on the Mac only. So it is prototyped as cheaply as possible first.
- **3c. The Python boundary.** Measure the no-op `_core` call again at 1 to 8 threads. If it still falls, find which object's reference count the threads share (the `Cache` object, a bound method, a cached type or a module-level constant) and try, in order: taking shared-object traffic out of the call path, and CPython's deferred reference counting on `Cache` and `FanoutCache` instances. The second is a `PyUnstable_*` API, compiled only into per-version wheels, which is every wheel shipped today. PyO3 0.30.1 has no wrapper for it and pyo3-ffi leaves the symbol out, so it would be a raw `extern` call: new `unsafe`, which makes that arm Full tier. It also changes behaviour: an object with deferred reference counting is freed only by the garbage collector, so a `Cache` that is dropped without `close()` would keep its connections until a collection. That touches D-018 and would need a test, a changelog entry and Michael's decision. If the no-op call no longer falls, this item is closed.
- **3d. The two shipped regressions.** Measure them again on 0.5.8. If they're still there and 3a or 3b doesn't remove them, look for the cause; if the cause is the fixed spin count, the adaptive spin that was built and shelved (`bench/results/adaptive-spin-2026-11-05.diff`) gets one more measurement against the new baseline. Any change there is in `sqlite_mutex`, so Full tier.
- **3e. `memoize` in Rust.** Low priority (Michael, 2026-11-13), and last. A memoized hit on 3.14 took 1,555 ns, about 540 ns of it in the Python wrapper. A Rust callable must keep diskcache's key shape and `__cache_key__`, copy what `functools.wraps` copies, implement `__get__` so decorated methods still bind, and keep the conformance suite's `memoize` cases green on both backends. The key bytes it builds must be identical to today's, pinned by a test against keys written by 0.5.8: different bytes would orphan every memoized entry on disk and make this a Format-tier change. It runs only if the steps above leave the time, and only if a first measurement of a prototype shows the wrapper near the 100 ns that was guessed.

Not in this plan: releasing the GIL during a read on GIL builds (plan 11, option 4; it helps threads on a GIL build, costs a single thread, and Talk Python runs one single-threaded worker), the in-process near-cache (on hold in the vision), and `abi3t` wheels.

**Step 4: the gate.** `bench.gate` already judges latency and the threaded mixes against diskcache. What it lacks is a check on oxide's own scaling and any `FanoutCache` or multi-process row. It gains:

- scaling rows on 3.14t: the 1,000 : 1 mix at 1, 2 and 4 threads on a plain `Cache` and a `FanoutCache(8)`, judged as a ratio to the same build's one-thread rate;
- the multi-process read rows from `bench/read_procs.py`, judged the same way;
- thresholds recorded per machine, since a ratio still depends on the core count and, on the Mac, on the mix of performance and efficiency cores. Each is set from this plan's final numbers with headroom for noise, with the evidence in a comment.

The roadmap's "add a benchmark suite to CI" is rewritten to say this, and the cross-cutting CI bullet is corrected to match how the project is actually checked (`scripts/check.sh`).

**Step 5: release.** Each kept experiment declares its tier by what it touches. 3a and 3b touch `sqlite`, `pool`, `store` or the gate, so they are Full: every sanitizer lane, the shuttle models, a 24-hour soak. 3c is Patch while it only moves shared-object traffic in the PyO3 layer, and Full if it adds the raw deferred-refcount call. 3d is Full. 3e is Patch as long as its key bytes are unchanged. The performance page in the user guide, the README's numbers and the roadmap page are updated from the final table, then `make docs`. Changes of different tiers are released separately, lower tier first, so a Patch-tier gain doesn't wait on a soak.

## Targets (proposed; confirmed after step 1)

On 3.14t, the 1,000 : 1 mix, one shared handle, each judged against the directory-per-thread control at the same thread count, which is what the machine can do when nothing is shared:

- `FanoutCache(8)` reaches at least 80% of the control at 2 and 3 threads on htz and at 4 on the Mac.
- A plain `Cache` reaches at least 60% of it. A plain `Cache` is one SQLite file, so its ceiling is lower by design, and the docs already send threaded apps to `FanoutCache`.
- One thread no slower than 0.5.8 on any gate row: the median of at least 7 interleaved rounds within the gate's 5%, on 3.14 and 3.14t.
- Neither shipped regression worse than today.

The earlier draft's "4 threads at least 1.5 times one thread" for a plain `Cache` was dropped: plan 12's numbers suggest it is already met (1,690k at 4 threads against about 1,075k at 1 in the Linux VM), so it couldn't fail. The percentages above are placeholders until step 1 shows where 0.5.8 stands; if it already meets them, the plan's work is the gate, 3c and 3e, and it says so.

## Risks

- Most of what is left to win sits inside SQLite's locking or beside the fork gate. Both are where this project's worst bugs have been, and plan 32 just finished hardening one of them. Every Full-tier change here is prototyped and measured before it is written properly, and a gain under 15% at 4 threads doesn't justify a Full-tier change.
- htz has few CPUs. Rows above its CPU count are reported as oversubscribed and carry no target.
- Timings need AC power and an idle machine, un-niced. No benchmark runs beside a soak or a sanitizer run.

## Log

- 2026-11-22, the-rust-architect's plan review: ready with changes, all folded in. It confirmed plan 12's numbers and the 93% and 7% split, and corrected three claims: `bench.gate` already judges the threaded mixes against diskcache, part of the Python-boundary work is already done (frozen pyclasses, no reference taken by the receiver), and a held read-mark lock gains less in threads than in processes. Required changes: the deferred-refcount arm is Full tier (a raw `extern`, so new `unsafe`) and changes when a dropped `Cache` releases its connections; the risk list for thread-affine connections (closed before a fork, counted by `poison_inherited`, no real `thread_local!` because of D-017 and D-018, the retire double-check, `Applied`, `check_usable`); and targets anchored to the directory-per-thread control, since the first draft's plain-`Cache` target was probably already met. It also set the tiers for 3d and 3e and noted that ratio thresholds still differ by machine.
