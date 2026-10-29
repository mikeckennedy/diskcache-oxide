#!/usr/bin/env bash
# Everything a CI job would run, locally. Usage: scripts/check.sh [--slow]
#
# Builds the extension into one environment per supported Python, then runs the Rust checks, the
# extension smoke test, stubtest, strict mypy, and the conformance suite against diskcache 5.6.3
# (the oracle) and against diskcache-oxide in each.
set -euo pipefail
cd "$(dirname "$0")/.."

SLOW=0
[[ "${1:-}" == "--slow" ]] && SLOW=1

# environment directory : uv Python request
ENVS=("venv:3.14" ".venv-ft:3.14t" ".venv-315:3.15" ".venv-315t:3.15t")

step() { printf '\n==> %s\n' "$*"; }
# The shipped SQLite flags from .cargo/config.toml, with memory statistics turned back on.
memstatus_flags() { sed -n 's/^LIBSQLITE3_FLAGS = "\(.*\)"$/\1/p' .cargo/config.toml | sed 's/DEFAULT_MEMSTATUS=0/DEFAULT_MEMSTATUS=1/'; }

step 'cargo fmt --check'
cargo fmt --all --check

step 'cargo clippy'
cargo clippy --all-targets --quiet -- -D warnings

step 'cargo test'
cargo test --quiet

step 'cargo deny check'
cargo deny --log-level error check

# The shuttle models of the fork gate and the write lock (scripts/model-check.sh).
step 'model check (shuttle)'
if (( SLOW )); then scripts/model-check.sh 1000000; else scripts/model-check.sh 100000; fi

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
opts = d.sqlite_compile_options()
assert "ENABLE_STAT4" not in opts and "DEFAULT_MEMSTATUS=0" in opts, f"built without the SQLite flags from .cargo/config.toml: {opts}"
build = "free-threaded" if not sys._is_gil_enabled() else "GIL"
print(f"diskcache_oxide {d.__version__} on {sys.version.split()[0]} ({build}), SQLite {d.sqlite_version()}")
'
    "$env/bin/python" -m mypy.stubtest diskcache_oxide --concise
    "$env/bin/python" -m mypy --strict --no-incremental python/diskcache_oxide

    # The suite against diskcache 5.6.3 (the behavior oracle), then against diskcache-oxide.
    "$env/bin/pytest" -q -p no:cacheprovider
    "$env/bin/pytest" -q -p no:cacheprovider --backend=oxide tests/conformance
    if (( SLOW )); then
        "$env/bin/pytest" -q -p no:cacheprovider -m slow
        "$env/bin/pytest" -q -p no:cacheprovider -m slow --backend=oxide tests/conformance
    fi
done

# The memory-instrumented build: SQLite's memory statistics on (they cost a process-wide mutex per
# allocation, so the shipped build turns them off). Free-threaded, since that's where memory grows
# with the thread count. Not editable, so its extension doesn't overwrite the 3.14t one in python/;
# its own target dir keeps the two SQLite builds from evicting each other.
step 'memory-instrumented build (3.14t, SQLITE_DEFAULT_MEMSTATUS=1)'
LIBSQLITE3_FLAGS="$(memstatus_flags)" CARGO_TARGET_DIR="$PWD/target/memstatus" \
    UV_PROJECT_ENVIRONMENT=.venv-mem uv sync --quiet --locked --no-editable --python 3.14t
DCO_REQUIRE_MEMSTATUS=1 .venv-mem/bin/pytest -q -p no:cacheprovider tests/unit/test_memory.py
# Memory numbers only; its throughput column is informational (take performance from bench.gate).
.venv-mem/bin/python -m bench.memory --threads 1,8,32 --seconds 1

step 'all checks passed'
