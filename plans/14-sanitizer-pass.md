# Plan 14: the sanitizer pass before 0.1.0

Michael's gate from 2026-11-04 (`plans/10-phase-3-talk-python-handoff.md`, section 3, item 6): run the sanitizers over the whole codebase, fix what they find, and only then run the final 24-hour soak, so that soak covers any fixes. Plan 12 made this more pressing: `sqlite_mutex.rs` (step 5a) replaces SQLite's own mutexes with pthread code of ours, 25 `unsafe` blocks across FFI that Miri can't execute. The deferred-owner stack (`stack.rs`) was checked alone on 2026-11-04 (Miri, 16 seeds; ThreadSanitizer); this pass covers everything.

Each step is small, reviewed by `the-rust-architect`, and committed and pushed when it lands. The deliverable is `scripts/sanitize.sh`, rerunnable before every release, plus a results section here.

## What runs where

| Lane | Tool | Covers | Where |
|---|---|---|---|
| A | Miri, `-Zmiri-many-seeds` | Core modules that never reach SQLite or the file system: `gate`, `genlock`, `stack`, `sync`, and whatever else a probe shows runs (step 1) | Mac or mini, native |
| B | ThreadSanitizer | All of core's tests, with the bundled SQLite compiled with `-fsanitize=thread`, including `tests/sqlite_mutex.rs` in debug and `--release` | macOS native (2a), and the Linux container natively on arm64 (2b) |
| C | AddressSanitizer, LeakSanitizer, and UBSan on SQLite's C | All of core's tests, SQLite compiled with `-fsanitize=address,undefined -fsanitize-trap=undefined` | Linux container, natively. Linux is LeakSanitizer's reference platform; on macOS it only runs through ASan and is flaky on arm64 |
| D | ThreadSanitizer through Python | The unit suite and conformance on 3.14t, against a CPython built `--disable-gil --with-thread-sanitizer`, with the extension and SQLite under TSan | Linux container |
| E | AddressSanitizer through Python | The same on 3.14 (GIL), with stock CPython and the ASan runtime preloaded | Linux container |

All of it runs on arm64. The sanitizers don't depend on the architecture, and arm64's weaker memory model is the harder test for races. x86-64 coverage stays with the native suite on `talk-python-htz` (plan 084, Step 12). The Linux lanes run in OrbStack's VM on the mini, which falls under the Linux-specific exception to running natively.

**Toolchains.** C built with a clang whose LLVM differs from rustc's can fail to link or report wrongly. The Linux image gains the clang whose major version matches `rustc +nightly -vV`'s LLVM (23 today), and the script refuses to run a C-instrumented lane if they differ. On macOS the probe used Apple clang 21.0.0 (clang-2100.4.1.1) against rustc's LLVM 23.1.5, and linked and reported correctly. The script records both versions in every run's summary.

## The probe so far (2026-11-06)

ThreadSanitizer on all core tests, macOS arm64, nightly 2026-11-03, `-Zbuild-std`, `CFLAGS_aarch64_apple_darwin="-fsanitize=thread -g"`:

- It builds and runs, and attributes frames inside SQLite and in our code, so lane B works on macOS.
- `allocation-size-too-big` in `spill::tests::a_size_too_big_to_allocate_is_out_of_memory` (the 2^54-byte request). That's TSan's allocator aborting where a real `malloc` returns NULL. Fix: `allocator_may_return_null=1` in the options, so the test runs and checks what it should.
- One data race, both top frames inside SQLite: `walIndexRecover` writing the WAL index in `-shm` memory against `walTryBeginRead`'s atomic read, reached from `open::tests::racing_creators_all_succeed_with_one_schema`. **Not yet known to be benign.** The reader side is benign by design: it reads the header without a lock, twice and checksummed, then takes a read lock and checks again. But within one process, `unixShmLock` serializes recovery with `pShmNode->pShmMutex`, a FAST mutex that comes from `mutex_alloc`, so it's ours, even though no frame of `sqlite_mutex` is on either stack. A broken mutex of ours would look exactly like this. Step 2a settles it.
- The run aborted at the first report, so most tests haven't run under TSan yet.

## Rules for reports

- **Races (TSan) aren't suppressed through `.supp` files.** A `race:` pattern matches its function in any frame of either stack, so it would also hide a race whose other side is ours. Instead `sanitize.sh` parses the log and allows a race only by signature: the top frame of each of the two accesses, both in SQLite, listed in `scripts/sanitize/allowed-races.txt` with the test that showed it and the reason. Any frame of ours on either stack fails the lane, whatever the signature.
- `.supp` files exist only for CPython's own TSan reports (lane D, citing CPython 3.14's `Tools/tsan/suppressions_free_threading.txt`) and for LeakSanitizer, if step 3 finds a leak that is deliberate. Each line names the report, the test that showed it, and the source that makes it benign.
- A report with one of our frames is a finding until shown otherwise, and showing otherwise goes through the architect.
- `halt_on_error=0`, so one report doesn't hide the rest. The lane fails on any report not allowed above.

## Steps

### Step 1: `scripts/sanitize.sh` and lane A (Miri)

The script takes `--lanes A,B,...` (default all) and `--quick` (fewer seeds), writes `sanitize-results/<timestamp>/` with a log per lane and a `summary.txt` (toolchain and clang versions, commit, per-lane verdict), and exits non-zero if any lane fails. It installs what it needs (`rustup component add miri rust-src` on nightly) and uses a target dir per lane (`target/san-<lane>`), so nothing it builds touches `target/`.

Miri: probe each core test module under `cargo +nightly miri test -p diskcache-oxide-core --lib <module>::`, and keep the ones that pass or fail only on Miri's own findings, not on an unsupported operation (FFI, `fork`, file locks). Expect `gate`, `genlock`, `stack`, `sync` and `lockwait`'s pure helpers. Run the kept list at `-Zmiri-many-seeds=0..32` (`0..4` with `--quick`). Shuttle models stay with `scripts/model-check.sh`.

**Done when:** lane A runs from the script, the module list and why each excluded module is out are written here, and its result is recorded.

### Step 2a: lane B on macOS, the WAL A/B first, and the mutex tests TSan can see

1. **The WAL A/B.** Run `racing_creators_all_succeed_with_one_schema` under TSan, many times, on `721de5a` (before step 5a, SQLite's own mutexes) and on HEAD. The `walIndexRecover`/`walTryBeginRead` report goes in `allowed-races.txt` only if it also appears on `721de5a`. If it appears only with our mutexes, it's a finding in `sqlite_mutex.rs`.
2. **Tests TSan can actually check.** `contended_increments_lose_nothing` counts with an `AtomicU64`, and TSan never reports a race on atomics, so it can't catch a mutex that fails to exclude. Add a test that guards a plain `UnsafeCell<u64>` with the mutex, through enter, try, and spin-then-trylock, on FAST and RECURSIVE, with the spin budget non-zero (3 or more CPUs, asserted).
3. **A canary.** An `#[ignore]` test, or `--cfg dco_tsan_canary`, where leave skips the unlock or two threads use different mutexes. The lane runs it and requires TSan to report it, proving the lane can see a race in our mutex at all.
4. **The run.** `RUSTFLAGS=-Zsanitizer=thread`, `-Zbuild-std`, SQLite's C flags gaining `-fsanitize=thread -g`, `TSAN_OPTIONS=halt_on_error=0 allocator_may_return_null=1 second_deadlock_stack=1`. TSan's lock-order detector covers our pthread mutexes, so no separate deadlock tool. The `sqlite_mutex` tests run in debug and `--release`, since debug adds owner tracking that changes the code. Core tests don't fork (`fork_timeout.rs` runs a subprocess with `Command`), so there's no fork rule here.

**Done when:** every core test runs to completion under TSan on macOS, the canary is caught, the A/B's verdict is written here, and any finding is fixed or brought back.

### Step 2b: lane B in the Linux container

The same as 2a's run on arm64 Linux, since `sqlite_mutex.rs` uses pthreads on both and glibc's mutex differs from macOS's. The image gains the matching clang (above).

**Done when:** as for 2a, on Linux.

### Step 3: lane C (AddressSanitizer, LeakSanitizer, UBSan on SQLite)

In the Linux container: `RUSTFLAGS=-Zsanitizer=address`, SQLite with `-fsanitize=address,undefined -fsanitize-trap=undefined -fno-omit-frame-pointer` (trap mode, since Rust doesn't link the UBSan runtime and `__ubsan_handle_*` would be undefined), `ASAN_OPTIONS=detect_leaks=1 allocator_may_return_null=1 detect_stack_use_after_return=1`. Start with no LeakSanitizer suppressions: memory still reachable from a global isn't reported as a leak, and core tests don't fork. Any report is a finding until shown otherwise.

**Done when:** every core test runs clean, or each report is accounted for here.

### Step 4a: lane D (ThreadSanitizer through free-threaded Python)

Build CPython 3.14.x from source in the container with `--disable-gil --with-thread-sanitizer`, cached in a named volume (`dco-cpython-san`). An uninstrumented free-threaded interpreter would flood the lane with false reports, since CPython's critical sections and `PyMutex` synchronize through atomics TSan can't see. Build the extension against it with `-Zsanitizer=thread` and SQLite under TSan, then run `pytest -p no:cacheprovider` (non-slow) and `--backend=oxide tests/conformance`. CPython's known reports use its own suppressions file. The py crate's own `unsafe` is small (three places), so this lane is for free-threading as a whole.

Forks happen here, in the Python tests. Run with `die_after_fork=0`. A report that appears only in a forked child is a lead to read, not an automatic failure.

**Done when:** the suite runs to completion with results recorded.

### Step 4b: lane E (AddressSanitizer through Python)

Stock CPython 3.14 (uv-managed), the extension and SQLite built under ASan, and the ASan runtime from rustc's LLVM preloaded with `LD_PRELOAD`, with `PYTHONMALLOC=malloc` and `ASAN_OPTIONS=detect_leaks=0` (CPython leaks by design at exit). No sanitized CPython build is needed. Forked children leave through `os._exit`, so they never reach a leak check either way.

**Done when:** the suite runs to completion with results recorded.

### Step 5: the overnight run and triage

`scripts/sanitize.sh` with all lanes on the mini overnight (one soak or sanitize run at a time there). Each finding gets a section here: the report, the cause, the fix (with the architect's review and a regression test that fails without it), or the reason it's allowed. Then `scripts/check.sh` on all four interpreters, and the 24-hour soak on that commit, run with `--fanout-threads 16` (plan 12, 2026-11-06).

## Out of scope

MemorySanitizer (it needs every byte instrumented, CPython included, for little beyond lane C); x86-64 sanitizer runs; sanitizing the soak itself.

## Progress

- 2026-11-06: plan written after a TSan probe on macOS (above). The architect's review: PASS with required changes, all applied. The WAL race's reason was wrong (`pShmMutex` is one of our mutexes), so it gets an A/B before it can be allowed. TSan races are allowed by signature, not `.supp`, so no frame of ours can hide. The mutex tests gain a non-atomic guarded counter and a canary, since TSan can't see a race on atomics. Lane E preloads ASan instead of building CPython. The fork rule moves to the Python lanes, since core tests don't fork. The clang version is pinned to rustc's LLVM. UBSan runs in trap mode. Steps 2 and 4 are split.
