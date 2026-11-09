#!/usr/bin/env bash
# Line coverage for the whole library: both Rust crates and the Python package, in one report.
#
# Usage: scripts/coverage.sh [--html] [pytest args...]
#
# Needs cargo-llvm-cov (`cargo install --locked cargo-llvm-cov`) and the llvm-tools rustup component.
# Most of diskcache-oxide-py is reachable only from Python, so `cargo test` alone undercounts it. This
# runs `cargo test`, then builds an instrumented copy of the extension, stages it beside a copy of
# python/diskcache_oxide, and runs the unit and oxide conformance suites against it with coverage.py
# on top. Every process that exits normally writes its own profile, and llvm-cov merges them. A forked
# child that leaves through os._exit skips the profile write, so code that runs only in fork children
# (e.g. lib.rs's _after_fork_in_child) reads as uncovered although the fork tests run it. Its own
# target dir (target/llvm-cov-target), so the normal build is never invalidated.
#
# Output: the summary tables on stdout, target/llvm-cov-target/lcov.info (Rust),
# target/llvm-cov-target/python-coverage.xml, and with --html target/llvm-cov-target/html/ plus
# target/llvm-cov-target/python-html/.
#
# The coverage build is unoptimised, so its stack frames are larger. The small-stack scenario that
# expects a deep value to decode on a reduced thread stack trips the RecursionError guard early, so
# it is deselected here; it runs in the normal suite.
set -euo pipefail
cd "$(dirname "$0")/.."

html=0
if [[ "${1:-}" == "--html" ]]; then
    html=1
    shift
fi

py="$PWD/venv/bin/python"
[[ -x "$py" ]] || { echo "venv/ is missing: UV_PROJECT_ENVIRONMENT=venv uv sync --locked --python 3.14" >&2; exit 1; }
cargo llvm-cov --version >/dev/null 2>&1 || { echo "cargo-llvm-cov is missing: cargo install --locked cargo-llvm-cov" >&2; exit 1; }
"$py" -c 'import coverage' 2>/dev/null || { echo "coverage is missing from venv/: UV_PROJECT_ENVIRONMENT=venv uv sync --locked" >&2; exit 1; }

export CARGO_TARGET_DIR="$PWD/target/llvm-cov-target"
export PYO3_PYTHON="$py"
eval "$(cargo llvm-cov show-env --sh 2>/dev/null)"
cargo llvm-cov clean --workspace

echo "== cargo test"
cargo test --workspace --quiet

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

echo "== pytest (unit + conformance against oxide)"
ulimit -n 10240 2>/dev/null || true
export COVERAGE_FILE="$CARGO_TARGET_DIR/.coverage"
rm -f "$COVERAGE_FILE"
status=0
PYTHONPATH="$stage" "$py" -m coverage run --source="$stage/diskcache_oxide" -m pytest -q -p no:cacheprovider \
    --backend=oxide tests/unit tests/conformance \
    --deselect 'tests/unit/test_stack.py::test_scenario[every-position]' "$@" || status=$?

echo
echo "== Rust"
cargo llvm-cov report --summary-only
cargo llvm-cov report --lcov --output-path "$CARGO_TARGET_DIR/lcov.info"
echo
echo "== Python"
"$py" -m coverage report
"$py" -m coverage xml -q -o "$CARGO_TARGET_DIR/python-coverage.xml"
if (( html )); then
    cargo llvm-cov report --html --output-dir "$CARGO_TARGET_DIR"
    "$py" -m coverage html -q -d "$CARGO_TARGET_DIR/python-html"
    echo "HTML: $CARGO_TARGET_DIR/html/index.html and $CARGO_TARGET_DIR/python-html/index.html"
fi

if (( status != 0 )); then
    echo "pytest failed (exit $status); the coverage above is from a partial run." >&2
fi
exit "$status"
