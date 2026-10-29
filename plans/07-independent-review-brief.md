# Independent review brief: the diskcache-oxide concurrency core

You're one of several reviewers, each working alone, asked to find bugs in the concurrency core of diskcache-oxide. Don't look for other reviewers' findings. Don't take this brief's claims on trust either: verify them against the code. Don't change the code; report.

**Review this commit:** `bae079d` on `main` (github.com/mikeckennedy/diskcache-oxide). If you're handed a later one, say which.

## What this is

diskcache-oxide is a clean-room Rust reimplementation of the Python library diskcache 5.6.3: a key-value cache on disk, built on SQLite, used by web apps from many threads and processes at once, including processes created with `fork()` (gunicorn and the like). Python calls a Rust core through PyO3. The core, `crates/diskcache-oxide-core`, has no Python in it, forbids `unsafe`, and builds with Rust 1.99.1. It must be correct on Python 3.14 and 3.15, with the GIL and free-threaded (no GIL), on macOS and Linux.

The failures that matter most, roughly in order:

1. **A deadlock or a hang:** a thread or process that never finishes, especially around `fork()`.
2. **A forked child that inherits a half-finished SQLite operation**, or SQLite lock state from a thread that doesn't exist in the child (the reason the fork gate exists; D-012).
3. **Lost or corrupted writes**, or a value served for the wrong key.
4. **A leak** of connections, file descriptors, counters or locks that slowly degrades a long-running process.
5. **Starvation:** one thread or operation that can be held off indefinitely.

## Where to look

The concurrency core, all under `crates/diskcache-oxide-core/src/`:

- `gate.rs`: the fork gate. Every SQLite call holds it in shared mode; the before-fork hook takes it exclusively. Its module docs state the rules.
- `genlock.rs`: the in-process write lock (with a starvation mode), the connection-slot counter, and process-generation tagging, so a forked child reads inherited locks as free.
- `store.rs`: the per-file `Store`, its registry and handle counting, the write path (`write_as`), held transactions (`begin_hold`) and the manual WAL checkpoint.
- `pool.rs`, `lockwait.rs` (busy waits and backoff), `poison.rs` (transactions inherited across fork), `spill.rs` (values in files), `maintenance.rs` (`clear`, `expire`, `cull`), `wal.rs`.
- In `crates/diskcache-oxide-py/src/`: `lib.rs` (the blocker that releases the Python interpreter during waits, and the fork hooks) and `cache.rs` (handle open, close and reuse).

Design context, to read after the code: `plans/04-design.md` sections 7 and 8; the "Concurrency rules" section of `plans/05-phase-2-core-cache.md`; and `tests/DIVERGENCES.md`, especially D-012, D-016 to D-020 and D-024.

## Changed most recently: look here hardest

These changed in the last day. Each was reviewed once and is tested, but fresh code is where bugs hide. `plans/06-phase-2.5-concurrency-review.md` (its Progress section) says why each changed.

- **The gate's counts** (`gate.rs`): holders and parked levels now share one `AtomicU64` (holders in the low half, parked as a wrapping i32 in the high half), read in one load; `begin_fork` re-reads its own levels on every poll. This fixed two races the model checker found.
- **Store registry and handle counting** (`store.rs`: `count_handle`, `claim`, `find`, `release_handle`, `add_handle`, `give_back`): a signed handle count whose landing point decides whether a store is live or retired, revival of a retired store instead of a second store, matching on the canonicalized directory, and closing connections returned to a retired store.
- **The "already detached" flag** (`py/src/lib.rs`: `detach`, `detached_wait`): the blocker skips re-attaching the interpreter on threads inside `crate::detach`.
- **The write lock:** `try_lock_nested` (barging at gate depth > 0), non-starving waiters sleeping while a starving one has the lock reserved, and RAII waiter counts (`Counted`).
- **`begin_hold` at gate depth > 0** fails at once with `WriteLockBusyInTransaction` instead of waiting.
- **`pop`** returns the value still encoded and decodes after `COMMIT`.
- **`clear()`/`expire()`** pause and yield between chunks only while a same-process writer waits.
- **`Store::checkpoint`:** retries only busy rounds, returns real errors, and counts one attempt per call.

## Rules the code claims to follow

Check that it does, everywhere:

- **Lock order:** the write lock, then the gate (shared), then a pool connection. No blocking wait while holding the gate, except SQLite's own short busy wait and bounded local work.
- **No thread waits on a gate holder while attached** to the Python interpreter (on a GIL build that deadlocks, since the holder may need the GIL to finish). Work run through the py crate's `detach` helper must never attach the interpreter.
- **At gate depth > 0** (a nested operation inside a held transaction), the write lock and connection slots are only ever tried, never waited for.
- **Across `fork()`:** the gate and the write lock use only atomics and thread-locals, so a child can't inherit a locked mutex. Every value a child might inherit held is tagged with a process generation and reads as free in the child.
- **One live `Store` per database file per process,** so threads of one process never meet SQLite's busy handler against each other.

## Known, and not what we're asking for

So you can skip them, or argue with them on purpose:

- **Deliberate choices:** WAL checkpoints are manual by default (D-024: no housekeeping on the write path unless the app asks). `cull()` has no yield between chunks (measured: 3.5× slower with one, for little gain). A writer in another process gets into a running `clear()` through SQLite's busy wait, usually, but not by guarantee.
- **Performance is out of scope unless it's a hang.** One open lead: lock waits sleep-poll at 50 µs, which costs 3 ms per poll on kernels with coarse timers (OrbStack's VM).
- **Already found and fixed, for you to check rather than rediscover:** the two gate races above, waiter counts leaking on unwind, and two `Store`s for one file after close and reuse.

## What's been tested

- **Unit tests:** 102 core unit tests, plus about 700 Python tests per interpreter, including a black-box conformance suite run against real diskcache 5.6.3.
- **Model checker:** 8 shuttle models of the gate and the write lock (`src/gate/model.rs`, `src/genlock/model.rs`), with up to 1M schedules per scheduler. Each has a guard that fails the run if no schedule reaches the state the model tests.
- **Soaks, both PASS:**
  - 8.4 h on macOS: 180k ops/s, 161 kills and 192 forks.
  - 6 h on Linux: 100k ops/s, 122 kills, 64 graceful restarts and 126 forks.

  Both had no problems, stalls or leaks. Note that soaks missed the two gate races; only the model checker found them.

## How to run things

- `cargo test -p diskcache-oxide-core`: the core's unit tests, in about 1 s.
- `scripts/model-check.sh [iterations]`: the shuttle models. **A new model is the best way to prove an interleaving bug**; copy the style of the existing ones (a `Witness`, `assert_idle`, a `Reach` guard). `DCO_MODEL_REPLAY=<schedule>` replays a failure.
- `scripts/check.sh`: everything (Rust checks, the models, every Python, the conformance suite), about 6 minutes on a laptop.
- `scripts/linux-check.sh`: the same checks in an Ubuntu container (OrbStack or Docker).
- `soak/`: the multi-hour soak harness.

## What to send back

One finding per entry, most severe first, in this format, so several reviews can be merged:

```
### <short title>
- Severity: critical (hang, corruption, or a fork child using inherited state) | high (leak or starvation in a realistic case) | medium (edge case) | low (doc, clarity)
- Where: file:line (and the function)
- Trigger: the interleaving or input, step by step, as concrete as you can
- Effect: what goes wrong, and who would see it
- Evidence: a failing test or model if you wrote one (paste it), otherwise your reasoning
- Confidence: certain | likely | possible
- Fix idea: optional, one or two lines
```

Then a short **"Checked and found sound"** list (each rule and file you went through), so coverage gaps are visible, and anything you **couldn't** check and why.
