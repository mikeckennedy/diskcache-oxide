# Plan 33: why a plain `Cache` reads slower with more processes (spike)

Status: done (2026-11-24). No build: Michael decided against the sentinel reader. The cause of the multi-process fall is the kernel's per-file advisory lock; it collapses on macOS and only bends past 4 processes on Linux, where the stop rule is met. Results: `bench/results/read-sentinel-2026-11-24.md`. One item left for Michael: the per-read `fstat` on a database opened with an empty WAL (+45% on Linux at one process once gone), whose fix is a write at open and a reworded D-019. Reviewed as a plan on 2026-11-22 by the-rust-architect (ready with changes, all folded in). A time-boxed spike: it measured, profiled and prototyped, and shipped nothing. Michael's decisions (2026-11-22): the real Linux numbers come from the htz host, and this runs before plan 34, which builds on its result.

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
- 2026-11-24, steps 1 and 2 (in part) and 4, on the Mac and htz. Results and every table: `bench/results/read-sentinel-2026-11-24.md`.
  - **Step 1.** `bench/scaling/fcntl_probe.py` is the bare-lock control. `bench/read_procs.py` has the controls (3 and 16 processes, a directory per process, diskcache's plain `Cache`, raw `sqlite3`, threads on 3.14t, a small value, a larger page cache, maintenance off, a WAL with frames, kernel time per worker), plus `--writes` for mixed loads and `--prototype` for the prototype's rows. The machine names itself `mac-studio-m5-max` (M5 Max, 18 CPUs); the 2026-11-18 file was labelled `macbook-m5-pro`, and its numbers match within noise.
  - **The cause.** On the Mac the bare lock falls from 1,810k pairs a second at 1 process to 301k at 8, and raw `sqlite3` and diskcache's plain `Cache` fall to the same 280k to 290k reads a second as oxide's. Nothing in oxide is in it.
  - **Step 2, the probe only.** Michael's decision: I run the 2-minute probe on htz, he runs the table later at a quiet hour (`bench/scaling/htz_read_procs.sh`). htz has 8 vCPUs now, not 4. One file peaks at 1,024k pairs a second at 3 processes and drifts to 708k at 8; a file per process reaches 4,146k. That is 72% and 63% of the control at 2 and 3 processes, so the bare lock misses the 80% stop rule on Linux, as a plateau and not a collapse. The cache table on htz hasn't run, so question 1 is half answered.
  - **A departure from the plan.** The stop rule took the directory-per-process row as the ceiling. It isn't: that row pays the same lock calls, about 40% of a read. A sentinel connection in the raw `sqlite3` control showed it (1,255k reads a second at one process against 774k, 14.5M at 16 processes, 1% kernel time), so candidate S was prototyped on the Mac without waiting for the htz table. Step 3's Linux profile was not done.
  - **Two side findings.** A database whose WAL index was built from an empty WAL makes SQLite call `fstat` at the start of every read until the first commit (18% of a one-process read in oxide). And the 256-page default page cache means `pread`s for this 351-page database, which contend across processes once the lock traffic is gone.
  - **Candidate S, the prototype** (`bench/results/read-sentinel-prototype-2026-11-24.diff`, built in a throwaway worktree, never merged, no fork handling). Reads only, WAL with frames: a plain `Cache` 1,139k at one process against 667k and 3,285k at four against 509k; with a page cache that fits, 16.2M at 16 processes. Threads on one handle: plus 60% at one thread, nothing at two or more.
  - **Its cost, measured.** A sentinel that gives its mark up only when its process notices a change let the WAL reach 114 MiB in 1.5 s. Down for its own process's writes, one process keeps the gain at both mixes with the WAL unchanged (1,067k against 703k at 1,000 : 1, 942k against 588k at 99 : 1). Across processes it trades the WAL for speed: with one writer and four pure readers, "quiet 64" completed 11 of 31 forced checkpoints at a 16 ms budget and reached 65 MiB with none forced, and "quiet 512" kept the WAL healthy with no gain.
  - **Candidates A, B and C were not built.** The review's judgment on B: reading `mxFrame` and `nBackfill` needs no VFS shim (`SQLITE_FCNTL_FILE_POINTER`, then `xShmMap`), but it is still a third `unsafe` module and it fixes neither an idle holder nor the checkpoint that runs inside a commit. Not worth it now.
- 2026-11-24, the-rust-architect's review of the findings (read only; it ran nothing). It confirmed the readings of SQLite 3.54.1 from source: the `fstat` path (`pagerPagecount`, `hdr.nPage` zero after `walIndexRecover` on an empty WAL, ended by the first commit, not brought back by a TRUNCATE with connections open), `unixShmLock`'s counting, the mark a reader picks, and that a read under a sentinel is never stale. Corrections: the extra write cost is a WAL sync and page copies, not a database sync; and "quiet 64 keeps the WAL at baseline" was luck, which the writer-and-readers run then showed. What a real build needs, from the review: the sentinel inside `PoolState` so `close_idle_all`, `files_with_connections` and `quiesce_all` see it (a child that inherits one reads with SQLite's count and no kernel lock, plan 32's class of bug); down whenever the in-process write lock is held, and around the background checkpoint; own commits told from foreign ones by a store-level commit counter; a quiet period counted in time and longer than a forced round after a foreign change; a thread that drops it in an idle process (the prototype's timer runs only inside a cache call, and `read_with_deadline` promises no clock read); unix only (Windows counts in-process holders only on remote paths); D-017's bound and D-018's close order reworded; shuttle models, every sanitizer lane with it on, new soak shapes (one writer with pure readers, a process that reads once and sleeps, a stopped holder), and off by default for one release. A stopped or frozen process holding a sentinel pins the WAL for everyone; that can be documented, not fixed. Its recommendation: correct the docs now; build only the conservative form (the gain promised for one process at any mix, and for several while the database is quiet; never worse than today on WAL size or checkpoint success), as its own Full-tier plan, after the htz table with the prototype on htz. It left to Michael whether about 0.55 µs a `get` is worth a connection that is always inside a transaction.
- 2026-11-24, the review's Mac gates, run. One writer with four pure readers: as above, the prediction held. Eight threads on 3.14t with 7 rounds: 980k with the prototype against 988k without, so no regression (the 3-round 819k against 871k was noise).
- 2026-11-24, step 2 on htz. Michael asked me to run it, so `bench/scaling/htz_read_procs.sh` ran over ssh in a temp directory, in Docker with the 0.5.8 wheel from PyPI; the temp directory and the Docker image it pulled were removed afterwards. Results in `bench/results/linux/htz-2026-11-24/` and section 6 of the results page. A plain `Cache` reaches 90% and 86% of the directory-per-process control at 2 and 3 processes (88% and 88% on 3.14t), so the stop rule is met: the collapse is macOS's, and on Linux the curve only bends past 4 processes. A sentinel would gain more there (+75% at one process in the raw `sqlite3` control), and so would the `fstat` fix (+45% in oxide at one process, against +18% on the Mac).
- 2026-11-24, Michael's decision: no sentinel. He judged it too complicated for what it gains, and unix only; I agreed. The deciding ratio: about half a microsecond a `get` on the Mac (about 3 µs on htz) against a connection always inside a transaction beside the fork gate, release rules whose failures show only as a growing WAL, and an idle thread. Earlier in the same interview, before the call to skip it, he had chosen "quiet-only across processes" as the scope if it were built, and "measure first" for the `fstat` fix. The prototype stays as `bench/results/read-sentinel-prototype-2026-11-24.diff`. Plan 34's item 3a closes with this. No release comes out of this plan (`scripts/release_tier.sh`: tier none); the docs corrections ship with the next docs deploy.
- Left for Michael: whether to fix the per-read `fstat` (a write when a process first opens a store, which rewords D-019's "an open writes nothing"; Full tier, since it touches `open`).
