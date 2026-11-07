#!/usr/bin/env bash
# The sanitizer pass (plans/14-sanitizer-pass.md), rerunnable before every release.
#
# Usage: scripts/sanitize.sh [--lanes A,B,C,D,E] [--quick]
#
#   --lanes   which lanes to run (default all five; a lane not built yet fails the run):
#               A  Miri on the core tests that never reach SQLite or fsync
#               B  ThreadSanitizer on core: every core test, the bundled SQLite instrumented too, races
#                  allowed only by signature (scripts/sanitize/allowed-races.txt), plus the canary and
#                  the mutex tests in release (plan 14, step 2a; macOS and Linux)
#               C  AddressSanitizer, LeakSanitizer and UBSan on core (step 3; not built yet)
#               D  ThreadSanitizer through a TSan-built free-threaded CPython (step 4a; not built yet)
#               E  AddressSanitizer through Python, runtime preloaded (step 4b; not built yet)
#   --quick   fewer Miri seeds (4 rather than 32)
#
# Results land in sanitize-results/<timestamp>/ (a log per lane and summary.txt), with
# sanitize-results/latest pointing at the newest. Each lane builds into its own target dir
# (target/san-<lane>), so nothing here touches target/. The nightly is pinned (NIGHTLY below, or
# DCO_SAN_NIGHTLY) so a run is reproducible. Exits non-zero if any lane fails or isn't built.
# Long runs belong on the mini, one soak or sanitize run at a time.
set -euo pipefail
cd "$(dirname "$0")/.."

LANES='A,B,C,D,E' QUICK=0
NIGHTLY="${DCO_SAN_NIGHTLY:-nightly-2026-11-04}"  # rustc 708c94ef0, the build plan 14's probes used
while (( $# )); do
    case "$1" in
        --lanes) LANES="$2"; shift ;;
        --quick) QUICK=1 ;;
        -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

[[ -x "$HOME/.cargo/bin/rustup" ]] && export PATH="$HOME/.cargo/bin:$PATH"
OUT="sanitize-results/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$OUT"
ln -sfn "$(basename "$OUT")" sanitize-results/latest
SUMMARY="$OUT/summary.txt"
FAILED=()

note() { printf '%s\n' "$*" | tee -a "$SUMMARY"; }
step() { printf '\n==> %s\n' "$*"; }

note "diskcache-oxide sanitize  $(date '+%Y-%m-%d %H:%M:%S')  commit $(git rev-parse --short HEAD)$(git diff --quiet || echo ' (dirty)')"
if ! rustup toolchain list | grep -q "^$NIGHTLY"; then
    rustup toolchain install "$NIGHTLY" --profile minimal -c miri,rust-src >/dev/null 2>&1 \
        || { note "setup FAIL: couldn't install $NIGHTLY"; exit 1; }
fi
note "host $(uname -sm), $NIGHTLY ($(rustc +"$NIGHTLY" --version | cut -d' ' -f2-)), LLVM $(rustc +"$NIGHTLY" -vV | sed -n 's/^LLVM version: //p'), cc $(cc --version | head -1)"
note "lanes requested $LANES$( (( QUICK )) && echo ', quick')"

# Lane A: Miri. Which tests it can run was found by running every core test alone under Miri
# (plan 14, step 1, with the coverage table there); the rest call into SQLite (`sqlite3_config` on
# first open) or need fsync on a real file. Two runs: isolation on (deterministic clock and RNG) for
# everything that allows it, and off for what needs the file system or wall time. Re-probe when a
# module gains tests.
MIRI_ISO_MODULES=(gate:: genlock:: poison:: error:: wal::)
MIRI_HOST_MODULES=(stack:: spill::)
# Not run under Miri: the 2^54-byte allocation it can't model (resource exhaustion), and three tests
# of real-time behavior (a 10 ms deadline; a check every 250 ms or 64 commits), which Miri's clock
# can't honor, since it follows instructions when isolated and runs about 1000x slow on the host's.
# All four run natively and under ThreadSanitizer (lane B). Plan 17 adds two (probed 2026-11-06):
# setting a directory's mtime needs `fsetattrlist`/`futimens`, which Miri doesn't support, and the
# 8-writer sweep race takes 82 s a seed under Miri (45 minutes at 32 seeds) for what TSan checks.
# Plan 19 adds a third, the dead temp file test, for the same mtime reason.
MIRI_SKIP=(
    spill::tests::a_size_too_big_to_allocate_is_out_of_memory
    spill::tests::a_sweep_with_a_min_age_skips_young_directories
    spill::tests::a_sweep_removes_only_the_temp_files_a_dead_writer_left
    spill::tests::writers_racing_a_sweeper_in_one_directory_never_fail_except_the_macos_rmdir_race
    genlock::tests::a_waiter_with_a_short_deadline_still_starves_in_time
    wal::tests::checks_are_rate_limited_by_time_or_commit_count
    wal::tests::a_successful_checkpoint_does_not_back_off
)
# -Zmiri-num-cpus=4: so genlock's and sqlite_mutex's spins (3+ CPUs) are what runs.
# -Zmiri-address-reuse-rate=1.0: a freed address is always offered to the next same-thread allocation
# of its size, so an ABA on addresses (plan 14, finding 1) surfaces at the first seed. The cross-thread
# rate stays at Miri's default: raising it induces synchronization between the threads, which masks
# data races and weak-memory bugs, the lane's other job; the stack gets its own pass below with both
# rates up. Never add -Zmiri-strict-provenance: `stack` relies on exposed provenance by design.
MIRI_FLAGS='-Zmiri-num-cpus=4 -Zmiri-address-reuse-rate=1.0'
MIRI_REUSE_FLAGS='-Zmiri-address-reuse-rate=1.0 -Zmiri-address-reuse-cross-thread-rate=1.0'

# Every test name the core lib has, from the Miri build itself. Printed one per line.
miri_list() {
    CARGO_TARGET_DIR="$PWD/target/san-A-$NIGHTLY" cargo "+$NIGHTLY" miri test -q -p diskcache-oxide-core --lib -- --list 2>/dev/null \
        | sed -n 's/: test$//p'
}

# The exact names for one run: each module's tests minus MIRI_SKIP, plus that mode's single tests.
# Fails, naming it, if a module matches no test or a single test doesn't exist.
miri_names() {  # miri_names <iso|host> <all-names-file> <module...>
    local mode="$1" all="$2"; shift 2
    local m t n
    for m in "$@"; do
        n=$(grep -c "^$m" "$all" || true)
        (( n > 0 )) || { echo "module $m matches no test" >&2; return 1; }
        grep "^$m" "$all" | grep -v -x -F -f <(printf '%s\n' "${MIRI_SKIP[@]}")
    done
    while read -r kind t; do
        [[ "$kind" == "$mode" ]] || continue
        grep -q -x -F "$t" "$all" || { echo "miri-tests.txt names $t, which doesn't exist" >&2; return 1; }
        echo "$t"
    done < <(grep -v '^#' scripts/sanitize/miri-tests.txt)
}

lane_A() {
    local seeds=32 log="$OUT/A-miri.log" all="$OUT/A-all-tests.txt" iso=() host=() t
    (( QUICK )) && seeds=4
    step "lane A: Miri, seeds 0..$seeds"
    if ! miri_list > "$all" || [[ ! -s "$all" ]]; then
        note "A  FAIL  setup: couldn't build or list the core tests under Miri"; FAILED+=(A); return
    fi
    while IFS= read -r t; do iso+=("$t"); done < <(miri_names iso "$all" "${MIRI_ISO_MODULES[@]}" 2>"$OUT/A-names.err")
    while IFS= read -r t; do host+=("$t"); done < <(miri_names host "$all" "${MIRI_HOST_MODULES[@]}" 2>>"$OUT/A-names.err")
    if [[ -s "$OUT/A-names.err" ]] || (( ${#iso[@]} == 0 || ${#host[@]} == 0 )); then
        note "A  FAIL  setup: $(head -1 "$OUT/A-names.err")"; FAILED+=(A); return
    fi
    printf '%s\n' "${iso[@]}" > "$OUT/A-iso-tests.txt"
    printf '%s\n' "${host[@]}" > "$OUT/A-host-tests.txt"
    # Leak checking stays on in both. Seeds run in parallel, so their output interleaves; the verdict
    # is cargo's exit code, and the summary counts the per-seed results.
    local rc=0
    CARGO_TARGET_DIR="$PWD/target/san-A-$NIGHTLY" MIRIFLAGS="$MIRI_FLAGS -Zmiri-many-seeds=0..$seeds" \
        cargo "+$NIGHTLY" miri test -p diskcache-oxide-core --lib -- --exact "${iso[@]}" >"$log" 2>&1 || rc=1
    CARGO_TARGET_DIR="$PWD/target/san-A-$NIGHTLY" MIRIFLAGS="$MIRI_FLAGS -Zmiri-disable-isolation -Zmiri-many-seeds=0..$seeds" \
        cargo "+$NIGHTLY" miri test -p diskcache-oxide-core --lib -- --exact "${host[@]}" >>"$log" 2>&1 || rc=1
    # The stack once more with cross-thread address reuse at 1.0 too (the finding's shape: a taker
    # frees, a pusher allocates), on a few seeds, where masking races doesn't matter.
    CARGO_TARGET_DIR="$PWD/target/san-A-$NIGHTLY" MIRIFLAGS="-Zmiri-num-cpus=4 $MIRI_REUSE_FLAGS -Zmiri-disable-isolation -Zmiri-many-seeds=0..4" \
        cargo "+$NIGHTLY" miri test -p diskcache-oxide-core --lib -- stack:: >>"$log" 2>&1 || rc=1
    # -Zmiri-many-seeds exits non-zero if any seed fails, so the exit code is the verdict; the seeds'
    # interleaved output can split even a `test result` line, so it isn't counted.
    if (( rc == 0 )); then
        note "A  PASS  ${#iso[@]} tests isolated and ${#host[@]} with the host, each clean on all $seeds seeds, and the stack with cross-thread address reuse on 4 (of $(wc -l < "$all" | tr -d ' ') core tests)"
    else
        # Report every failing seed and every distinct diagnostic (the two modes run one after the
        # other, and the first failure must not hide the second), not a line that merely names
        # `error::tests`.
        note "A  FAIL  see $log: $(grep -E '^FAILING SEED' "$log" | sort -u | tr '\n' ' ')"
        # A panic's message is on the line after `panicked at`, hence -A1. `|| true`: this grep is
        # a report, not the verdict, and a failure it can't name (a build error, an ICE) must not
        # end the script here under `set -e`.
        grep -E -A1 'Undefined Behavior|memory leaked|data race|panicked at|^error: (unsupported|resource|Undefined)' "$log" \
            | sed -E 's/^.*(error: Undefined Behavior|thread )/\1/' | cut -c1-200 | sort -u | sed 's/^/         /' | tee -a "$SUMMARY" || true
        FAILED+=(A)
    fi
}

# Lane B: ThreadSanitizer. Core built with `-Zsanitizer=thread` on a `-Zbuild-std` std, the bundled
# SQLite with `-fsanitize=thread -g` (the cc crate reads `CFLAGS_<target>`), so TSan attributes frames
# inside SQLite and in ours alike. Every core test runs (the lib's and tests/sqlite_mutex.rs), with
# `halt_on_error=0` so one report doesn't hide the rest and `exitcode=0` so the test binary's status
# is the tests' alone: the reports are judged afterwards by scripts/sanitize/tsan_races.py against
# allowed-races.txt (a data race whose two #0 frames form a listed pair, neither of them ours; our
# frames below, as SQLite's callers, are on every stack). Then the canary, a deliberate race in
# sqlite_mutex's tests that TSan must report, or the lane can't see our races at all. Then the
# sqlite_mutex tests again in release, where the debug owner tracking is gone. Core tests don't fork
# (fork_timeout runs a subprocess), so no fork rule here. `color=never`: TSan's report printer
# otherwise calls isatty(2) on every report, which with stderr in a log sets errno to ENOTTY on the
# reporting thread, between a syscall's failure and std's read of errno; the first run's one test
# failure (ENOTTY for EEXIST from a mkdir) was that.
TSAN_OPTIONS_LANE='halt_on_error=0 exitcode=0 color=never allocator_may_return_null=1 second_deadlock_stack=1'
TSAN_RACES=scripts/sanitize/tsan_races.py
TSAN_ALLOWED=scripts/sanitize/allowed-races.txt

run_tsan() {  # run_tsan <target> <command...>: the command with lane B's build and run environment
    local target="$1"
    shift
    (
        export CARGO_TARGET_DIR="$PWD/target/san-B-$NIGHTLY" RUSTFLAGS="-Zsanitizer=thread" TSAN_OPTIONS="$TSAN_OPTIONS_LANE"
        export "CFLAGS_${target//-/_}=-fsanitize=thread -g"
        "$@"
    )
}

lane_B() {
    local target log="$OUT/B-tsan.log" canary="$OUT/B-canary.log" release="$OUT/B-release.log" rc=0
    target=$(rustc "+$NIGHTLY" -vV | sed -n 's/^host: //p' || true)
    [[ -n "$target" ]] || { note "B  FAIL  setup: no host triple from rustc +$NIGHTLY"; FAILED+=(B); return; }
    step "lane B: ThreadSanitizer, $target"
    # One build, all of core's tests. `--target` is required with -Zbuild-std; it also keeps build
    # scripts and proc macros uninstrumented. One test at a time: each test's own threads are what
    # TSan should race, and a report is then attributable to one test. With tests in parallel, a new
    # thread can take an exited test thread's stack, whose TSD block holds errno on macOS, and TSan
    # reports std's errno accesses as a race between the two tests (allowed-races.txt). `--no-fail-fast`:
    # a failure in the lib binary must not stop tests/sqlite_mutex.rs from running.
    # `--tests`: the lib's unit tests and tests/sqlite_mutex.rs, not the doctests, which rustdoc
    # builds without the sanitizer and then refuses to link against the instrumented crate.
    run_tsan "$target" cargo "+$NIGHTLY" test -Zbuild-std --target "$target" -p diskcache-oxide-core --tests \
        --no-fail-fast -- --test-threads=1 >"$log" 2>&1 || rc=1
    if (( rc )) && ! grep -q '^test result:' "$log"; then
        note "B  FAIL  setup: the ThreadSanitizer build or run didn't get to the tests; see $log"; FAILED+=(B); return
    fi
    local failed_tests
    failed_tests=$( (grep -E '^test result: FAILED|^test .* FAILED$' "$log" || true) | head -5 | tr '\n' ' ')
    local verdict=0
    python3 "$TSAN_RACES" "$log" "$TSAN_ALLOWED" | tee -a "$SUMMARY" || verdict=1
    # The canary: run alone, must be reported.
    run_tsan "$target" cargo "+$NIGHTLY" test -Zbuild-std --target "$target" -p diskcache-oxide-core --lib \
        -- --ignored --exact sqlite_mutex::tests::canary_two_mutexes_exclude_nothing >"$canary" 2>&1 || true
    local canary_ok=0
    python3 "$TSAN_RACES" "$canary" "$TSAN_ALLOWED" --expect-ours | tee -a "$SUMMARY" || canary_ok=1
    # The mutex tests in release.
    local rel_rc=0
    run_tsan "$target" cargo "+$NIGHTLY" test -Zbuild-std --target "$target" --release -p diskcache-oxide-core \
        --lib -- --test-threads=1 sqlite_mutex:: >"$release" 2>&1 || rel_rc=1
    run_tsan "$target" cargo "+$NIGHTLY" test -Zbuild-std --target "$target" --release -p diskcache-oxide-core \
        --test sqlite_mutex -- --test-threads=1 >>"$release" 2>&1 || rel_rc=1
    local rel_verdict=0
    python3 "$TSAN_RACES" "$release" "$TSAN_ALLOWED" | tee -a "$SUMMARY" || rel_verdict=1
    if (( rc == 0 && verdict == 0 && canary_ok == 0 && rel_rc == 0 && rel_verdict == 0 )); then
        note "B  PASS  every core test ran under ThreadSanitizer on $target, races only as allowed, the canary reported, the mutex tests clean in release too"
    else
        note "B  FAIL  tests rc=$rc ($failed_tests) races=$([[ $verdict == 0 ]] && echo ok || echo bad) canary=$([[ $canary_ok == 0 ]] && echo seen || echo missed) release rc=$rel_rc races=$([[ $rel_verdict == 0 ]] && echo ok || echo bad); see $log, $canary, $release"
        FAILED+=(B)
    fi
}

IFS=',' read -r -a WANT <<< "$LANES"
for lane in "${WANT[@]}"; do
    case "$lane" in
        A) lane_A ;;
        B) lane_B ;;
        C|D|E) note "$lane  NOT BUILT  (plan 14)"; FAILED+=("$lane") ;;
        *) echo "unknown lane: $lane" >&2; exit 2 ;;
    esac
done

if (( ${#FAILED[@]} )); then
    note "FAILED: ${FAILED[*]}"
    exit 1
fi
note "lanes passed: $LANES"
