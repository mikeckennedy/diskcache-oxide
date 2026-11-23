# Plan 33: why a plain `Cache` reads slower with more processes (spike)

Status: draft (2026-11-22), reviewed once by the-rust-architect (ready with changes, all folded in; see the Log). A time-boxed spike: it measures, profiles and prototypes, and ships nothing. Anything it recommends building gets its own plan. Michael's decisions (2026-11-22): the real Linux numbers come from the htz host, and this runs before plan 34, which builds on its result.

## The problem

`bench/read_procs.py` (plan 31's step 6 review, MacBook Pro M5 Pro, 3.14, reads only, one thread per spawned process, 1,000 keys of a 1 KB str, no writer anywhere):

| Processes | oxide `Cache` | oxide `FanoutCache(8)` | diskcache `FanoutCache(8)` |
|---|---|---|---|
| 1 | 596k | 664k | 384k |
| 2 | 836k | 1,067k | 779k |
| 4 | 605k | 1,797k | 1,415k |
| 8 | 296k | 1,741k | 1,684k |

A plain `Cache` peaks at two processes and then falls to half its one-process rate. Eight files scale. So the limit is per database file, and it isn't the write lock, since nothing writes.

## What is known

From the code (the architect's review, SQLite 3.54.1 as bundled):

- A `get` is one autocommit `SELECT`, so one read transaction. SQLite's unix VFS takes a shared `fcntl` lock on a WAL read mark in the `-shm` file to start it and releases it to end it.
- `unixShmLock` keeps a count per process and makes the system call only when that count goes from 0 to 1 or from 1 to 0. So a process with one active reader pays two `fcntl` calls a read, which is every process in this benchmark, while readers that overlap inside one process skip them.
- The benchmark's database is 351 pages against the default 256-page cache, so it also does `pread`s. That costs about 6% at one process and nothing measurable at eight.

From a probe the architect ran during the review (Mac, AC power, 1 s points, 2 rounds: indicative, not a result):

| Processes | plain `Cache`, 1 KB | bare `fcntl` shared lock and unlock on one file (pairs/s) | the same, a file per process |
|---|---|---|---|
| 1 | 663k (39% system time) | 1,801k | 1,839k |
| 2 | 875k | 1,123k | 3,092k |
| 4 | 597k | 468k | 5,166k |
| 8 | 285k (95% system time) | 299k (99% system time) | 6,659k |

On macOS the fall is reproduced by nothing but processes taking and releasing a shared advisory lock on one file: 299k lock pairs a second at 8 processes against 285k `get`s a second. Value size and the page cache made no difference at 8 processes. So on the Mac the cause is the kernel's per-file advisory-lock path, with no SQLite and no oxide in the loop. The probe scripts are in the session's scratchpad and move into `bench/scaling/` in step 1.

Not known: whether Linux does the same. That is now the spike's first question, and it can be answered in minutes.

## Questions the spike answers

1. Does x86-64 Linux fall the same way on the bare lock, and on a plain `Cache`?
2. If it does, which change removes the lock traffic from an unchanged read, what does it cost in correctness and complexity, and is it worth building?
3. If it doesn't, what do the docs say about macOS, where most users develop and few deploy?

## Steps

**Step 1: controls.** Move the bare-`fcntl` probe into `bench/scaling/` as the first control: it needs no build. Then extend `bench/read_procs.py`, each row alternating with the others in a round as today:

- a 3-process row, and 16 processes on the Mac (18 CPUs);
- oxide `Cache` with a directory per process: the ceiling, and the yardstick for every threshold below;
- diskcache's plain `Cache`, and a raw `sqlite3` loop on one WAL file running the same lookup (`bench/scaling/raw_sqlite.py` has the shape);
- threads in one process at the same counts, on 3.14t, so the process and thread cases sit in one table;
- a small value that also fits the page cache, labelled as both, and a larger `sqlite_cache_size` on the 1 KB row, so `pread`s can be told from locking;
- background maintenance off (each process otherwise has a second thread);
- a WAL checkpointed before the run against one left with frames in it;
- per-worker user and system CPU time (`resource.getrusage`) beside the throughput.

**Step 2: the Linux measurement, on htz.** The bare-lock probe first, then the table. It needs its own script: `bench/scaling/htz_m2.sh` hard-codes `--cpus 8` and two old commits. The host has 4 CPUs (plan 10), so the conclusion rests on the 1, 2 and 3 rows; 4 is saturated and 8 is oversubscribed, and both are reported as such. Michael runs it or grants the access; nothing is installed on the host. The OrbStack VM is used for mechanism only, never for a number.

Decision point: compare a plain `Cache` with the directory-per-process control at the same process count. If on Linux it reaches 80% or more of the control at 2 and 3 processes, stop: the fall is a macOS trait. Write the result, correct the performance page, add the platform note, and close the spike.

**Step 3: the profile**, only if Linux falls. `strace -c -f` for system call counts and time per read, and `perf record -g` with kernel stacks in a privileged container, for the lock under `fcntl`. The same on the raw `sqlite3` control.

**Step 4: prototypes**, only if Linux falls. Each is gated by an environment variable on a scratch branch and never merged, as plan 12's read-ceiling prototypes were. In this order, cheapest first:

- **S. A sentinel connection, no `unsafe`.** One connection per store holds a read transaction open, which keeps the per-process lock count at 1 or more, so the other connections' reads skip the system call through SQLite's own counting. It is the cheapest test of whether removing the lock traffic gives the gain back. It pins the WAL like every candidate here, and its sentinel must live in a pool the fork hooks already see.
- **A. Keep the read-mark lock between reads.** A VFS shim over the unix VFS that swallows the release of a shared read-mark lock. The re-lock is then already free of both the system call and the mutex in SQLite's own code. The shim must let the old mark go when a read takes a different one.
- **B. Keep the read snapshot, and check for a change without a lock.** Hold a connection's snapshot across `get` calls and start a new one only when the WAL index header has changed. The header is in the first `-shm` region, which the shim sees through `xShmMap`. This also removes SQLite's per-file mutex from an unchanged read.
- **C. A bounded stale window**, with no change check. Measured only as the upper bound of what B can gain. It changes what a read returns across processes, so it could ship only as an opt-in with a divergence entry, and that would be Michael's decision.
- **D. Nothing.** `FanoutCache` already scales across processes and the docs recommend it for this shape. The result may be that a plain `Cache` keeps its limit, now with a measured cause.

Rules every prototype follows, from the review:

- **What a held mark costs.** It caps how far any process can checkpoint at that mark. A held lock on mark 0, which every reader takes when the WAL is fully backfilled, blocks all backfill in every process. So a held lock is not just "the WAL can't be reset".
- **Release is driven by change, not by idleness.** A busy reader is never idle, so an idle timer would pin the WAL for good. The lock or snapshot is given up when the WAL index header has changed, which is B's check; A needs it too.
- **B's capture order.** Copy the header before the snapshot begins. Copied after, a commit can land between the two and the read stays stale until the next commit. Compare all 48 bytes behind a memory barrier, treat any difference as changed, and end the snapshot before that connection writes.
- **Never open or close the `-shm` file ourselves.** Closing any descriptor on a file drops the process's POSIX locks on it. The `xShmMap` pointer is the only route to the header.
- **Fork.** `close_idle_all` and `poison_inherited` in `pool.rs` already handle a connection inherited with its lock record, provided the sentinel or any cached state lives in a pool those hooks see. An open snapshot on a pooled connection at a fork is plan 32's ground and must be ended by the before-fork hook.
- **The open rule.** Registering a VFS initializes SQLite, so it goes inside `sqlite.rs` after `ensure_installed`; `open_with_flags_and_vfs` is the precedent. A shim would be a third `unsafe` module, unix only, added to the grep test's allowlist, `lib.rs` and CLAUDE.md.

Out of scope: `locking_mode=EXCLUSIVE` (one process only; it belongs to the cache server idea), and the in-process near-cache in the vision.

**Step 5: the write-up.** For each candidate built: reads per second at each process count on both machines against the controls, the single-process cost, what happens to the WAL under a writer while readers hold marks (peak size and checkpoint success rate, as the soak's auto group measures them), and the code it would touch. Then a recommendation.

## What a win has to look like

A candidate is worth a build plan only if, on x86-64 Linux:

- a plain `Cache` at 2 and 3 processes reaches at least 80% of the directory-per-process control (the same yardstick as the stop rule);
- one process is no slower than today: the median of at least 7 interleaved rounds within the gate's own 5%, and `bench.gate` stays GO on 3.14 and 3.14t;
- threads in one process are no slower either, on the same statistic;
- a read is never stale: a test in which one process commits a `set` and another then reads must see the new value, every time, over many rounds;
- with a writer running, the WAL stays under `wal_size_limit` and forced checkpoints keep a success rate over 95%, the soak's own bar.

## What a build would cost

Any of S, A or B lands in `sqlite`, `pool`, `store` and beside the fork gate, and A and B add `unsafe`. That is the Full tier: every sanitizer lane, new shuttle models for whatever the fork hook must now end, and a 24-hour soak. None of it changes what is on disk. The recommendation weighs the gain against that, and against the fact that the affected users may all be on macOS.

## Deliverables

- The bare-lock probe in `bench/scaling/`, `bench/read_procs.py` with the controls, and the results for the Mac and htz in `bench/results/`.
- If prototypes were built: a results page with the profile and each prototype's numbers, and the prototypes as a `.diff` beside it.
- The Log below, the roadmap's Phase 4 finding rewritten from "likely the `-shm` read lock, not yet profiled" to what was found, and the same correction on the user guide's performance page (then `make docs`).
- A recommendation, and an interview with Michael on it.

Time box: one session to the decision point, two more if Linux falls.

## Log

- 2026-11-22, the-rust-architect's plan review: ready with changes, all folded in. It checked the "two `fcntl` calls a read" claim against SQLite's source and narrowed it to a process with one active reader, corrected the htz reference (plan 10, not plan 14) and the reuse of `htz_m2.sh`, and found candidate A's cost understated (a held mark caps the checkpoint, and mark 0 blocks all backfill). Its required changes: the bare-`fcntl` control first, release driven by a header change and not by idleness, B's capture order, never touching the `-shm` descriptor, a stop rule and win thresholds anchored to the directory-per-process control, and a staleness test. It added the sentinel-connection prototype and several controls. During the review it ran the bare-lock probe on the Mac, which reproduces the fall with no SQLite in the loop (the table above); those numbers are indicative and are rerun properly in step 1.
