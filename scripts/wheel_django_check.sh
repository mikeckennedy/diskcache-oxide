#!/usr/bin/env bash
# Install each built wheel in a clean venv and check the Django backend from the wheel alone (plan 31,
# step 9). Usage: scripts/wheel_django_check.sh dist/*.whl
#
# Per wheel, for the Python its tag names (cp314 -> 3.14, cp314t -> 3.14t, and so on):
#   1. without Django: `import diskcache_oxide` works and loads no django or asgiref module;
#   2. with the `django` extra: `'BACKEND': 'diskcache_oxide.DjangoCache'` set and get through
#      django.core.cache, on a plain alias and on `OPTIONS = {'shards': 8}`, from outside the repo,
#      so nothing is imported from the source tree.
# Runs on the host, so give it wheels for the host's platform.
set -euo pipefail
(( $# )) || { echo "usage: $0 WHEEL..." >&2; exit 2; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

for wheel in "$@"; do
    wheel="$(cd "$(dirname "$wheel")" && pwd)/$(basename "$wheel")"
    name="$(basename "$wheel")"
    tag="$(sed -E 's/.*-(cp3[0-9]+t?)-[^-]+\.whl$/\1/' <<<"$name")"
    [[ "$tag" =~ ^cp3[0-9]+t?$ ]] || { echo "$name: can't tell its Python from the tag" >&2; exit 1; }
    python="3.${tag#cp3}"
    printf '\n==> %s (Python %s)\n' "$name" "$python"

    venv="$work/$tag"
    UV_PYTHON_PREFERENCE=only-managed uv venv --quiet --python "$python" "$venv"
    uv pip install --quiet --python "$venv/bin/python" "$wheel"
    (cd "$work" && "$venv/bin/python" -I -W error -c '
import sys
import diskcache_oxide
loaded = sorted(m for m in sys.modules if m.split(".")[0] in ("django", "asgiref"))
assert not loaded, f"import diskcache_oxide loaded {loaded}"
assert "DjangoCache" not in diskcache_oxide.__all__
print(f"without Django: diskcache_oxide {diskcache_oxide.__version__} imports no Django")
')

    uv pip install --quiet --python "$venv/bin/python" "${wheel}[django]"
    (cd "$work" && "$venv/bin/python" -I -W error -c '
import sys, tempfile
import diskcache_oxide
assert diskcache_oxide.__file__.startswith(sys.prefix), f"imported from {diskcache_oxide.__file__}"
from django.conf import settings
root = tempfile.mkdtemp()
settings.configure(CACHES={
    "default": {"BACKEND": "diskcache_oxide.DjangoCache", "LOCATION": f"{root}/plain"},
    "sharded": {"BACKEND": "diskcache_oxide.DjangoCache", "LOCATION": f"{root}/sharded", "OPTIONS": {"shards": 8}},
})
import django
django.setup()
from django.core.cache import caches
for alias in ("default", "sharded"):
    cache = caches[alias]
    cache.set("k", {"v": [1, 2.5, "x"]})
    assert cache.get("k") == {"v": [1, 2.5, "x"]}, alias
    assert cache.add("n", 0) and cache.incr("n") == 1, alias
    assert type(cache).__module__ == "diskcache_oxide.djangocache", type(cache)
from diskcache_oxide import djangocache
djangocache.close_all()
print(f"with Django {django.__version__}: DjangoCache works, plain and shards=8")
')
done
printf '\n==> every wheel passed\n'
