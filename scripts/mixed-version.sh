#!/usr/bin/env bash
# Runs tests/unit/test_mixed_versions.py: 0.1.2 and this build on one cache directory (0.1.2 refuses
# format 3; this build opens its own cache in diskcache-oxide-format-3 beside 0.1.2's; and
# remove_old_formats() with a 0.1.2 worker still running never touches the new cache).
#
# Usage: scripts/mixed-version.sh [wheel-or-requirement]
#   default: the shipped 0.1.2 wheel for this platform and CPython 3.14, from dist/0.1.2/ or dist/.
#   0.1.2 was never on PyPI, so without a wheel there's nothing to install. The old version gets its
#   own venv in target/mixed-version/ (never the project's).
set -euo pipefail
cd "$(dirname "$0")/.."

old="${1:-}"
if [[ -z "$old" ]]; then
    # Chosen by platform tag: uv venvs have no pip, so `pip debug` can't be asked.
    case "$(uname -s)-$(uname -m)" in
        Darwin-arm64) platform='macosx_*_arm64' ;;
        Linux-x86_64 | Linux-aarch64) platform="manylinux_*_$(uname -m)" ;;
        *) platform='' ;;
    esac
    shopt -s nullglob
    if [[ -n "$platform" ]]; then
        for wheel in dist/0.1.2/diskcache_oxide-0.1.2-cp314-cp314-$platform.whl \
            dist/diskcache_oxide-0.1.2-cp314-cp314-$platform.whl; do
            old="$wheel"
            break
        done
    fi
    shopt -u nullglob
    if [[ -z "$old" ]]; then
        echo "no diskcache-oxide 0.1.2 wheel for $(uname -s) $(uname -m) and CPython 3.14 in dist/0.1.2/ or dist/;" \
            "pass one: scripts/mixed-version.sh path/to/diskcache_oxide-0.1.2-....whl" >&2
        exit 1
    fi
fi
echo "old side from: $old"
env_dir="$PWD/target/mixed-version"
rm -rf "$env_dir"
uv venv -q --python 3.14 "$env_dir"
uv pip install -q --python "$env_dir/bin/python" "$old"
"$env_dir/bin/python" -c 'import diskcache_oxide; print("old side:", diskcache_oxide.__version__)'
ulimit -n 10240 2>/dev/null || true
DCO_OLD_PYTHON="$env_dir/bin/python" venv/bin/pytest -q -p no:cacheprovider tests/unit/test_mixed_versions.py
