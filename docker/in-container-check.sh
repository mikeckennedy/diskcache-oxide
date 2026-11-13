#!/usr/bin/env bash
# Runs inside the dco-linux-dev container; started by scripts/linux-check.sh, not by hand.
#
# The repo is mounted read-only at /src. This copies a snapshot to /work/repo (skipping host venvs,
# target dirs and caches), then runs the scripts/check.sh steps against the copy. Cargo's registry,
# the target dirs and uv's cache live in named volumes, so nothing here touches the host's builds.
#
# Settings arrive as environment variables:
#   DCO_PYTHONS  comma-separated uv Python requests (default 3.14,3.14t,3.15,3.15t)
#   DCO_SLOW     1 adds the slow tests
#   DCO_BENCH    1 runs the latency gate on 3.14 and 3.14t, writing to /out
#   DCO_RUST     0 skips fmt, clippy, cargo test and deny
#   DCO_TESTS    0 skips the per-Python smoke test, ty and pytest (envs are still synced)
#   DCO_EXEC     a shell command run in the repo copy once per Python, with $VENV and $PY set
#   DCO_ROUNDS   gate rounds (default 4)
#   DCO_EMULATED set (by --platform, when it isn't the host's architecture) under emulation: tests
#                that can't run under Rosetta skip. They check that it's set, not its value.
set -uo pipefail

PYTHONS="${DCO_PYTHONS:-3.14,3.14t,3.15,3.15t}"
SLOW="${DCO_SLOW:-0}"
BENCH="${DCO_BENCH:-0}"
RUST="${DCO_RUST:-1}"
TESTS="${DCO_TESTS:-1}"
ROUNDS="${DCO_ROUNDS:-4}"
REPO=/work/repo

step() { printf '\n==> %s\n' "$*"; }
FAILED=()
run() {  # run <label> <command...>: keep going on failure, report at the end
    local label="$1"; shift
    if ! "$@"; then
        FAILED+=("$label")
        printf '!!! FAILED: %s\n' "$label"
    fi
}

# Same layout as scripts/check.sh, so commands copied from there work unchanged.
env_dir() {
    case "$1" in
        3.14) echo venv ;;
        3.14t) echo .venv-ft ;;
        3.15) echo .venv-315 ;;
        3.15t) echo .venv-315t ;;
        *) echo ".venv-${1//./}" ;;
    esac
}

step "snapshot /src -> $REPO"
rm -rf "$REPO" && mkdir -p "$REPO"
# Python's tarfile unpacks, not GNU tar: under OrbStack's Rosetta (--platform linux/amd64 on an
# arm64 Mac), GNU tar fails every directory and file it creates below the top level ("Function not
# implemented"). Creating the archive works there. filter="tar", not the stricter "data": it still
# refuses absolute names, `..` and setuid bits, but allows a symlink that points outside the copy,
# which a repo can hold.
tar -C /src -cf - \
    --exclude=./target --exclude=./dist --exclude=./soak-results \
    --exclude='./venv' --exclude='./venv*' --exclude='./.venv*' \
    --exclude='*/__pycache__' --exclude=./.pytest_cache \
    --exclude=./.ruff_cache --exclude=./.hypothesis --exclude='./python/diskcache_oxide/*.so' \
    . | "$(uv python find 3.14)" -c '
import sys, tarfile
with tarfile.open(fileobj=sys.stdin.buffer, mode="r|") as archive:
    archive.extractall(sys.argv[1], filter="tar")
' "$REPO" || { echo "snapshot failed" >&2; exit 1; }
[[ -f "$REPO/Cargo.toml" ]] || { echo "snapshot is missing Cargo.toml" >&2; exit 1; }
cd "$REPO" || exit 1
# dco_env: environments are built with `uv pip install`, never `uv sync`.
source scripts/uv_env.sh
printf '%s, %s CPUs, kernel %s, user %s\n' "$(. /etc/os-release && echo "$PRETTY_NAME")" "$(nproc)" "$(uname -r)" "$(id -un)"

# The workspace build. Per-Python extension builds get their own target dirs below, so switching
# interpreters never invalidates PyO3's build cache.
export CARGO_TARGET_DIR=/cache/target/workspace
# The image has no system python3, so tell pyo3's build script which interpreter the workspace
# build (clippy, cargo test) links against.
PYO3_PYTHON="$(uv python find 3.14)" && export PYO3_PYTHON

if [[ "$RUST" == 1 && -z "${DCO_EXEC:-}" ]]; then
    step 'cargo fmt --check'
    run 'cargo fmt' cargo fmt --all --check
    step 'cargo clippy'
    run 'cargo clippy' cargo clippy --all-targets --quiet -- -D warnings
    step 'cargo test'
    run 'cargo test' cargo test --quiet
    step 'cargo deny check'
    run 'cargo deny' cargo deny --log-level error check
fi

sync_env() {  # sync_env <python> <env>
    CARGO_TARGET_DIR="/cache/target/py-$1" dco_env "$2" "$1" || return 1
    # Optional: lets tests/proctitle.py name the processes (dco-pytest[...], dco-bench[...]).
    uv pip install --quiet --python "$2/bin/python" setproctitle || true
}

check_symbols() {  # check_symbols <env>
    local so defined undefined
    so="$("$1/bin/python" -c 'import diskcache_oxide._core as c; print(c.__file__)')" || return 1
    defined="$(nm -D --defined-only "$so" | grep -c sqlite3_)"
    undefined="$(nm -D --undefined-only "$so" | grep -c sqlite3_)"
    printf '%s: %s sqlite3_ symbols exported, %s imported\n' "$(basename "$so")" "$defined" "$undefined"
    nm -D --defined-only "$so" | grep -q ' PyInit__core$' || { echo "PyInit__core is not exported" >&2; return 1; }
    [[ "$defined" == 0 && "$undefined" == 0 ]]
}

IFS=',' read -r -a PYS <<< "$PYTHONS"
for py in "${PYS[@]}"; do
    env="$(env_dir "$py")"
    step "build on Python $py ($env)"
    if ! sync_env "$py" "$env"; then
        FAILED+=("build env $py")
        continue
    fi

    if [[ -n "${DCO_EXEC:-}" ]]; then
        step "exec on Python $py: $DCO_EXEC"
        run "exec $py" env VENV="$env" PY="$py" bash -c "$DCO_EXEC"
        continue
    fi
    [[ "$TESTS" == 1 ]] || continue

    step "smoke test on Python $py"
    run "smoke $py" "$env/bin/python" -W error -c '
import sys
import diskcache_oxide as d
assert d.sqlite_version() == d.BUNDLED_SQLITE_VERSION, "linked the system SQLite, not the bundled one"
assert issubclass(d.Timeout, TimeoutError) and issubclass(d.UnsafeValueError, TypeError)
opts = d.sqlite_compile_options()
assert "ENABLE_STAT4" not in opts and "DEFAULT_MEMSTATUS=0" in opts, f"built without the SQLite flags from .cargo/config.toml: {opts}"
build = "free-threaded" if not sys._is_gil_enabled() else "GIL"
print(f"diskcache_oxide {d.__version__} on {sys.version.split()[0]} ({build}), SQLite {d.sqlite_version()}")
'
    # The bundled SQLite stays private to _core.so (plan 12 step 5a: its mutex methods must be the
    # only ones, and Python's sqlite3 must not see them): no sqlite3_ symbol exported, and none
    # imported either, which would mean a system SQLite was linked instead of the bundled one.
    step "exported symbols on Python $py"
    run "nm $py" check_symbols "$env"
    step "ty on Python $py"
    run "ty $py" "$env/bin/ty" check --python "$env" python/diskcache_oxide
    # The suite against diskcache 5.6.3 (the behavior oracle), then against diskcache-oxide.
    step "pytest on Python $py"
    run "pytest $py" "$env/bin/pytest" -q -p no:cacheprovider
    step "pytest --backend=oxide on Python $py"
    run "pytest oxide $py" "$env/bin/pytest" -q -p no:cacheprovider --backend=oxide tests/conformance
    if [[ "$SLOW" == 1 ]]; then
        step "pytest -m slow on Python $py"
        run "pytest slow $py" "$env/bin/pytest" -q -p no:cacheprovider -m slow
        step "pytest -m slow --backend=oxide on Python $py"
        run "pytest slow oxide $py" "$env/bin/pytest" -q -p no:cacheprovider -m slow --backend=oxide tests/conformance
    fi
done

# The memory-instrumented build (see scripts/check.sh): SQLite memory statistics on, 3.14t only.
if [[ "$TESTS" == 1 && -z "${DCO_EXEC:-}" ]]; then
    step 'memory-instrumented build (3.14t, SQLITE_DEFAULT_MEMSTATUS=1)'
    # The shipped SQLite flags from .cargo/config.toml, with memory statistics turned back on.
    mem_flags="$(sed -n 's/^LIBSQLITE3_FLAGS = "\(.*\)"$/\1/p' .cargo/config.toml | sed 's/DEFAULT_MEMSTATUS=0/DEFAULT_MEMSTATUS=1/')"
    if LIBSQLITE3_FLAGS="$mem_flags" CARGO_TARGET_DIR=/cache/target/py-3.14t-memstatus DCO_EDITABLE=0 \
        dco_env .venv-mem 3.14t; then
        run 'memory tests' env DCO_REQUIRE_MEMSTATUS=1 .venv-mem/bin/pytest -q -p no:cacheprovider tests/unit/test_memory.py
        run 'memory report' .venv-mem/bin/python -m bench.memory --threads 1,8,32 --seconds 1
    else
        FAILED+=('build env .venv-mem')
    fi
fi

if [[ "$BENCH" == 1 ]]; then
    for py in 3.14 3.14t; do
        env="$(env_dir "$py")"
        if [[ ! -x "$env/bin/python" ]] && ! sync_env "$py" "$env"; then
            FAILED+=("build env $py (bench)")
            continue
        fi
        step "bench.gate --rounds $ROUNDS on Python $py"
        out="/out/gate-$py.txt"
        run "gate $py" bash -c "set -o pipefail; '$env/bin/python' -m bench.gate --rounds '$ROUNDS' 2>&1 | tee '$out'"
    done
fi

if (( ${#FAILED[@]} )); then
    step "FAILED: ${FAILED[*]}"
    exit 1
fi
step 'all checks passed'
