#!/usr/bin/env bash
# Builds diskcache-oxide for Python 3.14 and 3.14t, then runs the soak. See soak/README.md.
#
# Usage: soak/run.sh [--hours 24] [--minutes N] [any other soak.soak option, e.g. --procs 8]
#        With no duration, runs for 24 hours.
set -euo pipefail
cd "$(dirname "$0")/.."
# dco_env: environments are built with `uv pip install`, never `uv sync`.
source scripts/uv_env.sh

need() {  # need <command> <how to install it>
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "soak: '$1' is missing. Install it with:" >&2
        echo "    $2" >&2
        exit 1
    fi
}
# Prefer rustup's toolchain proxies over any other Rust on PATH (Homebrew's cargo, for example,
# ignores rust-toolchain.toml).
[[ -x "$HOME/.cargo/bin/rustup" ]] && export PATH="$HOME/.cargo/bin:$PATH"
need uv 'curl -LsSf https://astral.sh/uv/install.sh | sh    (then open a new shell)'
need rustup "curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal    (then open a new shell)"
if [[ "$(uname)" == Darwin ]] && ! xcode-select -p >/dev/null 2>&1; then
    echo "soak: the Xcode command line tools are missing (the bundled SQLite needs a C compiler). Install them with:" >&2
    echo "    xcode-select --install" >&2
    exit 1
fi

# rust-toolchain.toml pins the Rust version; rustup installs it on first use if needed.
echo "==> Rust $(rustup show active-toolchain 2>/dev/null | head -1)"

ENVS=(".venv-soak:3.14" ".venv-soak-ft:3.14t")
PYTHONS=()
for spec in "${ENVS[@]}"; do
    env="${spec%%:*}"
    py="${spec##*:}"
    echo "==> build for Python $py ($env)"
    dco_env "$env" "$py"
    uv pip install --quiet --python "$env/bin/python" psutil
    "$env/bin/python" -W error -c '
import sys
import diskcache_oxide as d
assert not d._core._DEBUG_BUILD, "a debug build; the soak needs the release build"
opts = d.sqlite_compile_options()
assert "ENABLE_STAT4" not in opts and "DEFAULT_MEMSTATUS=0" in opts, f"built without the SQLite flags from .cargo/config.toml: {opts}"
build = "free-threaded" if not sys._is_gil_enabled() else "GIL"
print(f"    diskcache_oxide {d.__version__} on {sys.version.split()[0]} ({build}), SQLite {d.sqlite_version()}")
'
    PYTHONS+=("$PWD/$env/bin/python")
done

ARGS=("$@")
if [[ ! " $* " =~ " --hours " && ! " $* " =~ " --minutes " ]]; then
    ARGS+=(--hours 24)
fi
pythons="$(IFS=,; echo "${PYTHONS[*]}")"
echo "==> soak ${ARGS[*]}"
# Keep the Mac awake for the whole run (idle and system sleep; the display may sleep).
keep_awake=()
[[ "$(uname)" == Darwin ]] && keep_awake=(caffeinate -i -s)
exec "${keep_awake[@]}" .venv-soak/bin/python -m soak.soak --pythons "$pythons" "${ARGS[@]}"
