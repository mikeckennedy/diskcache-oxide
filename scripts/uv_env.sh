# Builds or refreshes a project environment with `uv pip install`, never `uv sync`, which uninstalls
# whatever isn't in the groups it syncs (the docs tooling, ipykernel, anything installed by hand).
# Sourced by scripts/check.sh, scripts/sanitize.sh, soak/run.sh and docker/in-container-check.sh.
#
#   dco_env <env dir> <python> [extra `uv pip install` args for the project]
#
# Creates <env dir> with uv venv if it has no interpreter yet, installs the dependencies at the
# versions uv.lock pins (the default groups, dev and community, through `uv export --locked`, which
# fails on an out-of-date lock as `uv sync --locked` did), then the project itself, editable, with
# no dependencies of its own to resolve. DCO_EDITABLE=0 installs the project non-editable (the
# memory-instrumented build, whose extension mustn't land in python/). Pass
# `--reinstall-package diskcache-oxide` to rebuild an extension whose sources didn't change (a
# build under different RUSTFLAGS). Environment variables set on the call (CARGO_TARGET_DIR,
# LIBSQLITE3_FLAGS, RUSTFLAGS) reach the build.
dco_env() {
    local env="$1" python="$2"
    shift 2
    if [[ ! -x "$env/bin/python" ]]; then
        uv venv --quiet --python "$python" "$env" || return 1
    fi
    uv export --quiet --locked --no-hashes --no-emit-project --format requirements-txt \
        | uv pip install --quiet --python "$env/bin/python" -r - || return 1
    if [[ "${DCO_EDITABLE:-1}" == 1 ]]; then
        uv pip install --quiet --python "$env/bin/python" --no-deps "$@" -e .
    else
        uv pip install --quiet --python "$env/bin/python" --no-deps "$@" .
    fi
}
