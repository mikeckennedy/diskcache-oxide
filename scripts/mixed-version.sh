#!/usr/bin/env bash
# Runs tests/unit/test_mixed_versions.py: 0.1.2 beside this build on one cache directory.
#
# Usage: scripts/mixed-version.sh [wheel-or-requirement]
#   default: the shipped wheel in dist/0.1.2/ for this platform and 3.14, else `diskcache-oxide==0.1.2`
#   from PyPI. The old version gets its own venv in target/mixed-version/ (never the project's).
set -euo pipefail
cd "$(dirname "$0")/.."

old="${1:-}"
if [[ -z "$old" ]]; then
    for wheel in dist/0.1.2/diskcache_oxide-0.1.2-cp314-cp314-*.whl; do
        if venv/bin/python -m pip debug --verbose 2>/dev/null | grep -q "$(basename "$wheel" | sed -E 's/.*-cp314-cp314-(.*)\.whl/\1/')"; then
            old="$wheel"
        fi
    done
    old="${old:-diskcache-oxide==0.1.2}"
fi
env_dir="$PWD/target/mixed-version"
rm -rf "$env_dir"
uv venv -q --python 3.14 "$env_dir"
uv pip install -q --python "$env_dir/bin/python" "$old"
"$env_dir/bin/python" -c 'import diskcache_oxide; print("old side:", diskcache_oxide.__version__)'
ulimit -n 10240 2>/dev/null || true
DCO_OLD_PYTHON="$env_dir/bin/python" venv/bin/pytest -q -p no:cacheprovider tests/unit/test_mixed_versions.py
