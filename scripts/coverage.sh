#!/usr/bin/env bash
# Line coverage for the whole library: both Rust crates and the Python package, in one report.
#
# Usage: scripts/coverage.sh [--html] [--ft] [--fail-under N] [pytest args...]
#   --ft             on 3.14t, from .venv-ft/
#   --fail-under N   fail if Rust or Python line coverage is below N percent (scripts/check.sh --coverage)
#
# Needs cargo-llvm-cov (`cargo install --locked cargo-llvm-cov`) and the llvm-tools rustup component.
# Most of diskcache-oxide-py is reachable only from Python, so `cargo test` alone undercounts it. This
# runs `cargo test`, then builds an instrumented copy of the extension, stages it beside a copy of
# python/diskcache_oxide, and runs the unit and oxide conformance suites against it with coverage.py
# on top. Every process writes its own profile, and llvm-cov merges them. A process that leaves
# through os._exit (every forked child the tests make) would skip the profile write, so the staged
# sitecustomize.py wraps os._exit to call the coverage build's `_core._write_coverage()` first; it
# reaches every Python process the tests start, since they inherit PYTHONPATH. Its own target dir
# (target/llvm-cov-target, or target/llvm-cov-target-ft for --ft), so the normal build is never
# invalidated. --ft measures the free-threaded build, whose code paths differ in places.
#
# Output (in the target dir): the summary tables on stdout, lcov.info (Rust), python-coverage.xml,
# and with --html html/ plus python-html/.
#
# The coverage build is unoptimised, so its stack frames are larger. The small-stack scenario that
# expects a deep value to decode on a reduced thread stack trips the RecursionError guard early, so
# it is deselected here; it runs in the normal suite.
set -euo pipefail
cd "$(dirname "$0")/.."

html=0
floor=0
env_dir=venv
target="$PWD/target/llvm-cov-target"
while [[ $# -gt 0 ]]; do
    case "$1" in
        --html) html=1; shift ;;
        --ft) env_dir=.venv-ft; target="$PWD/target/llvm-cov-target-ft"; shift ;;
        --fail-under) floor="$2"; shift 2 ;;
        *) break ;;
    esac
done

py="$PWD/$env_dir/bin/python"
[[ -x "$py" ]] || { echo "$env_dir/ is missing: see CLAUDE.md's Environments" >&2; exit 1; }
cargo llvm-cov --version >/dev/null 2>&1 || { echo "cargo-llvm-cov is missing: cargo install --locked cargo-llvm-cov" >&2; exit 1; }
"$py" -c 'import coverage' 2>/dev/null || { echo "coverage is missing from $env_dir/: build it with dco_env (scripts/uv_env.sh)" >&2; exit 1; }

export CARGO_TARGET_DIR="$target"
export PYO3_PYTHON="$py"
eval "$(cargo llvm-cov show-env --sh 2>/dev/null)"
cargo llvm-cov clean --workspace

echo "== cargo test"
# A failing test (a timing test on a busy machine) is reported at the end, not a reason to skip the
# Python run: the coverage is still worth seeing.
rust_status=0
cargo test --workspace --quiet || rust_status=$?

echo "== instrumented extension"
link_args=()
if [[ "$(uname -s)" == "Darwin" ]]; then
    # maturin normally adds this: Python's symbols resolve at import, not at link.
    link_args=(-- -C link-arg=-undefined -C link-arg=dynamic_lookup)
fi
cargo rustc -p diskcache-oxide-py --lib --quiet "${link_args[@]}"

stage="$CARGO_TARGET_DIR/pkg"
rm -rf "$stage"
mkdir -p "$stage"
cp -R python/diskcache_oxide "$stage/"
rm -f "$stage"/diskcache_oxide/_core.*.so
built=$(find "$CARGO_TARGET_DIR/debug" -maxdepth 1 \( -name 'lib_core.dylib' -o -name 'lib_core.so' \) | head -1)
suffix=$("$py" -c 'import sysconfig; print(sysconfig.get_config_var("EXT_SUFFIX"))')
cp "$built" "$stage/diskcache_oxide/_core$suffix"
cat > "$stage/sitecustomize.py" <<'PY'
"""Coverage runs only (scripts/coverage.sh): a process leaving through os._exit writes its profile first."""

import os
import sys

_exit = os._exit


def _exit_writing_coverage(status: int, /) -> None:
    core = sys.modules.get('diskcache_oxide._core')
    if core is not None and hasattr(core, '_write_coverage'):
        core._write_coverage()
    _exit(status)


os._exit = _exit_writing_coverage
PY

echo "== pytest (unit + conformance against oxide)"
ulimit -n 10240 2>/dev/null || true
export COVERAGE_FILE="$CARGO_TARGET_DIR/.coverage"
rm -f "$COVERAGE_FILE"
py_status=0
PYTHONPATH="$stage" "$py" -m coverage run --source="$stage/diskcache_oxide" -m pytest -q -p no:cacheprovider \
    --backend=oxide tests/unit tests/conformance \
    --deselect 'tests/unit/test_stack.py::test_scenario[every-position]' "$@" || py_status=$?

echo
echo "== Rust"
floor_status=0
cargo llvm-cov report --summary-only --fail-under-lines "$floor" || floor_status=$?
cargo llvm-cov report --lcov --output-path "$CARGO_TARGET_DIR/lcov.info"
echo
echo "== Python"
"$py" -m coverage report --fail-under="$floor" || floor_status=$?
"$py" -m coverage xml -q -o "$CARGO_TARGET_DIR/python-coverage.xml"
if (( html )); then
    cargo llvm-cov report --html --output-dir "$CARGO_TARGET_DIR"
    "$py" -m coverage html -q -d "$CARGO_TARGET_DIR/python-html"
    echo "HTML: $CARGO_TARGET_DIR/html/index.html and $CARGO_TARGET_DIR/python-html/index.html"
fi

if (( rust_status != 0 )); then
    echo "cargo test failed (exit $rust_status); the coverage above is from a partial run." >&2
fi
if (( py_status != 0 )); then
    echo "pytest failed (exit $py_status); the coverage above is from a partial run." >&2
fi
if (( floor_status != 0 )); then
    echo "line coverage is below the ${floor}% floor." >&2
fi
(( rust_status == 0 && py_status == 0 && floor_status == 0 ))
