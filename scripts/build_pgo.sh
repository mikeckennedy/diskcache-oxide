#!/usr/bin/env bash
# Profile-guided release wheels, into dist/. Usage: scripts/build_pgo.sh [python ...]
#
# For each Python (default: 3.14 3.14t 3.15 3.15t): build instrumented, train on bench.baseline's
# workloads plus bench.pgo_train in a throwaway venv, merge the profiles, rebuild with them, and put
# the wheel in dist/. The GIL and free-threaded builds compile different code, so each gets its own
# profile. Each OS and architecture gets its own work dir under target/pgo/, so the Mac and the
# Linux containers (scripts/build_linux.sh) can share a checkout.
#
# This optimizes the Rust code only. The bundled SQLite is C, built by the system compiler, whose
# LLVM and profile format don't match rustc's, so it builds as in a normal release.
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHONS=("$@")
(( ${#PYTHONS[@]} )) || PYTHONS=(3.14 3.14t 3.15 3.15t)

toolchain=$(rustup show active-toolchain | cut -d' ' -f1)
rustup component add llvm-tools --toolchain "$toolchain" >/dev/null 2>&1
profdata=$(find "$(rustc --print sysroot)/lib/rustlib" -name llvm-profdata | head -1)
[[ -x "$profdata" ]] || { echo "llvm-profdata not found in $toolchain" >&2; exit 1; }

work="$PWD/target/pgo/$(uname -s)-$(uname -m)"
maturin() { uvx --from 'maturin>=1.15,<2' maturin "$@"; }
step() { printf '\n==> %s\n' "$*"; }

for python in "${PYTHONS[@]}"; do
    root="$work/$python"
    rm -rf "$root"
    mkdir -p "$root/profiles" "$root/wheel"

    step "$python: instrumented build"
    CARGO_TARGET_DIR="$work/target-generate" RUSTFLAGS="-Cprofile-generate=$root/profiles" \
        maturin build --release --quiet --interpreter "$python" --out "$root/wheel" 2>&1 | grep -vE '^\s*$' | tail -2

    step "$python: training"
    UV_PYTHON_PREFERENCE=only-managed uv venv --quiet --python "$python" "$root/venv"
    uv pip install --quiet --python "$root/venv/bin/python" "$root"/wheel/*.whl 'diskcache==5.6.3' setproctitle pytest
    LLVM_PROFILE_FILE="$root/profiles/train_%m_%p.profraw" "$root/venv/bin/python" -m bench.baseline \
        --backend oxide --quick --out "$root/baseline.json" >/dev/null
    LLVM_PROFILE_FILE="$root/profiles/train_%m_%p.profraw" "$root/venv/bin/python" -m bench.pgo_train
    # Only the training runs. The instrumented build's own build scripts and proc-macros also drop
    # default_*.profraw files here, and they profile the compiler's helpers, not the extension.
    count=$(find "$root/profiles" -name 'train_*.profraw' | wc -l | tr -d ' ')
    (( count > 0 )) || { echo "$python: training wrote no profiles" >&2; exit 1; }
    "$profdata" merge -o "$root/merged.profdata" "$root"/profiles/train_*.profraw
    echo "merged $count profiles"

    step "$python: optimized build"
    CARGO_TARGET_DIR="$work/target-use" RUSTFLAGS="-Cprofile-use=$root/merged.profdata -Cllvm-args=-pgo-warn-missing-function=false" \
        maturin build --release --quiet --interpreter "$python" --out dist 2>&1 | grep -vE '^\s*$' | tail -2
done

step 'dist/'
command ls -la dist/*.whl
