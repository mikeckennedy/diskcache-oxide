#!/usr/bin/env bash
# The sanitizer pass (plans/14-sanitizer-pass.md), rerunnable before every release.
#
# Usage: scripts/sanitize.sh [--lanes A,B,C,D,E] [--quick]
#
#   --lanes   which lanes to run (default all of the built ones):
#               A  Miri on the core tests that never reach SQLite, files it can't model, or fork
#               B  ThreadSanitizer on core (plan 14, step 2; not built yet)
#               C  AddressSanitizer, LeakSanitizer and UBSan on core (step 3; not built yet)
#               D  ThreadSanitizer through a TSan-built free-threaded CPython (step 4a; not built yet)
#               E  AddressSanitizer through Python, runtime preloaded (step 4b; not built yet)
#   --quick   fewer Miri seeds (4 rather than 32)
#
# Results land in sanitize-results/<timestamp>/ (a log per lane and summary.txt), with
# sanitize-results/latest pointing at the newest. Each lane builds into its own target dir
# (target/san-<lane>), so nothing here touches target/. Exits non-zero if any lane fails or a
# requested lane isn't built yet. Long runs belong on the mini, one soak or sanitize run at a time.
set -euo pipefail
cd "$(dirname "$0")/.."

LANES='A' QUICK=0
while (( $# )); do
    case "$1" in
        --lanes) LANES="$2"; shift ;;
        --quick) QUICK=1 ;;
        -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
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
note "host $(uname -sm), nightly $(rustc +nightly --version | cut -d' ' -f2-), LLVM $(rustc +nightly -vV | sed -n 's/^LLVM version: //p'), cc $(cc --version | head -1)"
note "lanes $LANES$( (( QUICK )) && echo ', quick')"

# Lane A: Miri. The modules and single tests it can run were found by probing every core test
# under Miri (plan 14, step 1); the rest call into SQLite (`sqlite3_config` on first open), need
# fsync on a real file, or fork. Re-probe when a module gains tests.
MIRI_MODULES=(gate:: genlock:: stack:: poison:: error:: wal:: spill::)
# Miri can't model a failed 2^54-byte allocation (resource exhaustion), the case this test proves.
MIRI_SKIP=(spill::tests::a_size_too_big_to_allocate_is_out_of_memory)

lane_A() {
    local seeds=32 log="$OUT/A-miri.log" skips=()
    (( QUICK )) && seeds=4
    for t in "${MIRI_SKIP[@]}"; do skips+=(--skip "$t"); done
    local singles=()
    while IFS= read -r t; do [[ -n "$t" && "$t" != \#* ]] && singles+=("$t"); done < scripts/sanitize/miri-tests.txt
    rustup component add --toolchain nightly miri rust-src >/dev/null 2>&1
    step "lane A: Miri, seeds 0..$seeds, ${#MIRI_MODULES[@]} whole modules and ${#singles[@]} single tests"
    # Isolation off: the spill, wal and open tests use temp files. Leak checking stays on. Two runs,
    # since libtest's --exact can't mix with module prefixes.
    export CARGO_TARGET_DIR="$PWD/target/san-miri" MIRIFLAGS="-Zmiri-disable-isolation -Zmiri-many-seeds=0..$seeds"
    if cargo +nightly miri test -p diskcache-oxide-core --lib -- "${MIRI_MODULES[@]}" "${skips[@]}" >"$log" 2>&1 \
        && cargo +nightly miri test -p diskcache-oxide-core --lib -- --exact "${singles[@]}" >>"$log" 2>&1; then
        note "A  PASS  $(grep -c '^test result: ok' "$log") seed runs: $(grep '^test result' "$log" | head -1 | sed 's/; 0 ignored.*//'), then $(grep '^test result' "$log" | tail -1 | sed 's/; 0 ignored.*//')"
    else
        # Seeds run in parallel and their output interleaves, so report the failing seed and the
        # first diagnostic Miri or a test prints, not a line that merely names `error::tests`.
        note "A  FAIL  see $log: $(grep -m1 -E '^FAILING SEED' "$log") $(grep -m1 -E 'Undefined Behavior|memory leaked|data race|panicked at|^error: (unsupported|resource|Undefined)' "$log" | cut -c1-160)"
        FAILED+=(A)
    fi
}

IFS=',' read -r -a WANT <<< "$LANES"
for lane in "${WANT[@]}"; do
    case "$lane" in
        A) lane_A ;;
        B|C|D|E) note "$lane  NOT BUILT  (plan 14)"; FAILED+=("$lane") ;;
        *) echo "unknown lane: $lane" >&2; exit 2 ;;
    esac
done

if (( ${#FAILED[@]} )); then
    note "FAILED: ${FAILED[*]}"
    exit 1
fi
note 'all lanes passed'
