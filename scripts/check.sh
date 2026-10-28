#!/usr/bin/env bash
# Everything a CI job would run, locally. Usage: scripts/check.sh [--slow]
#
# Builds the extension into one environment per supported Python, then runs the Rust checks, the
# extension smoke test, stubtest and the conformance suite against diskcache 5.6.3 in each.
set -euo pipefail
cd "$(dirname "$0")/.."

SLOW=0
[[ "${1:-}" == "--slow" ]] && SLOW=1

# environment directory : uv Python request
ENVS=("venv:3.14" ".venv-ft:3.14t" ".venv-315:3.15" ".venv-315t:3.15t")

step() { printf '\n==> %s\n' "$*"; }

step 'cargo fmt --check'
cargo fmt --all --check

step 'cargo clippy'
cargo clippy --all-targets --quiet -- -D warnings

step 'cargo test'
cargo test --quiet

step 'cargo deny check'
cargo deny --log-level error check

for spec in "${ENVS[@]}"; do
    env="${spec%%:*}"
    python="${spec##*:}"

    step "build + test on Python $python ($env)"
    UV_PROJECT_ENVIRONMENT="$env" uv sync --quiet --locked --python "$python"

    # -W error also catches CPython's warning if the extension ever re-enables the GIL.
    "$env/bin/python" -W error -c '
import sys
import diskcache_oxide as d
assert d.sqlite_version() == d.BUNDLED_SQLITE_VERSION, "linked the system SQLite, not the bundled one"
assert issubclass(d.Timeout, TimeoutError) and issubclass(d.UnsafeValueError, TypeError)
build = "free-threaded" if not sys._is_gil_enabled() else "GIL"
print(f"diskcache_oxide {d.__version__} on {sys.version.split()[0]} ({build}), SQLite {d.sqlite_version()}")
'
    "$env/bin/python" -m mypy.stubtest diskcache_oxide --concise

    "$env/bin/pytest" -q -p no:cacheprovider
    if (( SLOW )); then
        "$env/bin/pytest" -q -p no:cacheprovider -m slow
    fi
done

step 'all checks passed'
