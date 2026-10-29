# Independent review brief: the diskcache-oxide concurrency core

You're one of several reviewers, each working alone, asked to find bugs in the concurrency core of diskcache-oxide. Please don't look for other reviewers' findings, and don't take this brief's own claims on trust: verify them against the code.

## What this is

diskcache-oxide is a clean-room Rust reimplementation of the Python library diskcache 5.6.3: a key-value cache on disk, built on SQLite, used by web apps from many threads and processes at once, including processes created with `fork()`. Python calls a Rust core through PyO3. The core is `crates/diskcache-oxide-core`; it has no Python in it, forbids `unsafe`, and builds with the latest stable Rust (1.99.1). It must be correct on Python 3.14 and 3.15, both with the GIL and free-threaded (no GIL), on macOS and Linux.

The failures that matter most, roughly in order:

1. **A deadlock or a hang:** a thread or process that never finishes, especially around `fork()`.
2. **A forked child that inherits a half-finished SQLite operation**, or SQLite lock state from a thread that doesn't exist in the child (the reason the fork gate exists; D-012).
3. **Lost or corrupted writes**, or a value served for the wrong key.
4. **A leak** of connections, file descriptors, counters or locks that slowly degrades a long-running process.
5. **Starvation:** one thread or operation that can be held off indefinitely.

## Where to look

Read these first:

- `crates/diskcache-oxide-core/src/gate.rs`: the fork gate. Every SQLite call holds it in shared mode, and the before-fork hook takes it exclusively. Its module docs state the rules.
- `crates/diskcache-oxide-core/src/genlock.rs`: the in-process write lock (with a starvation mode), the connection-slot counter, and generation tagging, so a forked child reads inherited locks as free.
- `crates/diskcache-oxide-core/src/store.rs`: the per-file `Store`, its registry, handle counting, the write path (`write_as`), held transactions (`begin_hold`) and the manual WAL checkpoint.
- `crates/diskcache-oxide-core/src/pool.rs`, `lockwait.rs`, `poison.rs`, `spill.rs`, `maintenance.rs`, `wal.rs`.
- `crates/diskcache-oxide-py/src/lib.rs` and `cache.rs`: the blocker that releases the Python interpreter during waits, the fork hooks (`os.register_at_fork`), and handle open and close.

Then the design, as context only:

- `plans/04-design.md` sections 7 and 8, and the "Concurrency rules" section of `plans/05-phase-2-core-cache.md`.
- `tests/DIVERGENCES.md`: deliberate behavior changes from diskcache, especially D-012, D-016 to D-020 and D-024.

## Rules the code claims to follow

Check that it does, everywhere:

- **Lock order:** the write lock, then the gate (shared), then a pool connection. No blocking wait while holding the gate, except SQLite's own short busy wait and bounded local work.
- **No thread waits on a gate holder while attached** to the Python interpreter (on a GIL build that's a deadlock, since the holder may need the GIL to finish).
- **At gate depth > 0** (a nested operation inside a held transaction), the write lock and connection slots are only ever tried, never waited for.
- **Across `fork()`:** the gate and the write lock use only atomics and thread-locals, so a child can't inherit a locked mutex. Every value a child might inherit held is tagged with a process generation, and reads as free in the child.

## Known and already fixed

These came from earlier review rounds; you don't need to rediscover them, but do check the fixes:

- Two fork-gate races found by the model checker: `begin_fork` read its own levels once, before waiting, and read the holders and parked counts in two loads instead of one snapshot. Both are fixed (one packed word, own levels re-read on every poll).
- The write lock's waiter counts leaked if a wait unwound (now RAII guards).
- Two `Store`s for one file after close and reuse, or a close/open race (now decided under the registry, with a signed handle count).

## How to run things

- `scripts/check.sh`: everything (Rust checks, the model checker, every Python, the conformance suite). About 6 minutes on a laptop.
- `cargo test -p diskcache-oxide-core`: the core's unit tests, in about 1 s.
- `scripts/model-check.sh [iterations]`: shuttle models of the gate and the write lock (`src/gate/model.rs`, `src/genlock/model.rs`). A new model is the best way to prove an interleaving bug.
- `scripts/linux-check.sh`: the same checks in an Ubuntu container (OrbStack or Docker).
- `soak/`: the multi-hour soak harness (random kills, forks, graceful restarts and maintenance under load, with invariant checks).

## What to send back

For each finding: the file and line, the interleaving or input that triggers it (as concrete as you can, ideally a failing test or model), what goes wrong, and how sure you are. Rank them most severe first. Say what you checked and found sound too, so coverage gaps are visible. Don't change the code.
