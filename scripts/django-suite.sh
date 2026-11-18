#!/usr/bin/env bash
# Django's own cache tests against diskcache_oxide.DjangoCache, each Django line from its pinned sdist.
#
#   scripts/django-suite.sh [--env DIR] [line ...]
#
#   --env DIR  the environment to run in (default venv/). It needs the `django` dependency group, for
#              Django's own dependencies (asgiref, sqlparse); the installed Django itself is never used.
#   line       5.2, 6.0 or 6.1. Default: every line that supports the environment's Python.
#
# Each sdist is downloaded once into .django-src/ (ignored). Every run checks the tarball against its
# sha256 here and extracts it afresh into a directory of its own, so the tests never run against a different Django than they
# shipped with, edited or not. django_suite/run.py runs the tests in-process from the sdist; see its
# docstring for why.
set -euo pipefail
cd "$(dirname "$0")/.."

# The only place the Django versions are named. line, exact version, sdist sha256, newest Python.
VERSIONS=(
    "5.2 5.2.19 ad03d8abc51dbff95ebf34b27123cf0c26a3b62db7ba0cd520783686362533d1 3.14"
    "6.0 6.0.10 6005d030df924f85088454c18bd35dcf082b058aa355ad3df520d8de9aef9f41 3.14"
    "6.1 6.1.3 b5d2d9e35295065d13b1927b3f19a59bd119f737aa6299def14ccca1ae3c8c0d 3.15"
)
SRC=.django-src

env=venv
lines=()
while (( $# )); do
    case "$1" in
        --env) [[ $# -ge 2 ]] || { echo '--env needs a directory' >&2; exit 2; }; env="$2"; shift 2 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *) lines+=("$1"); shift ;;
    esac
done
known=" $(for row in "${VERSIONS[@]}"; do printf '%s ' "${row%% *}"; done)"
for line in "${lines[@]+"${lines[@]}"}"; do
    [[ "$known" == *" $line "* ]] || { echo "unknown Django line: $line (known:$known)" >&2; exit 2; }
done
python="$env/bin/python"
[[ -x "$python" ]] || { echo "no interpreter at $python" >&2; exit 2; }
pyver="$("$python" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
# For messages only: 3.14t on a free-threaded build (pyver stays numeric for the version checks).
pylabel="$pyver$("$python" -c 'import sysconfig; print("t" if sysconfig.get_config_var("Py_GIL_DISABLED") else "")')"

# The fd-heavy tests need more than macOS's default 256 over non-interactive ssh.
ulimit -n 10240

# Each run extracts into its own directory, so two runs at once never share a tree.
mkdir -p "$SRC"
# A run killed with SIGKILL leaves its tree (about 65 MB a line); sweep those older than a day.
find "$SRC" -maxdepth 1 -name 'run.*' -type d -mtime +0 -exec rm -rf {} + 2>/dev/null || true
RUN_DIR="$(mktemp -d "$PWD/$SRC/run.XXXXXX")"
trap 'rm -rf "$RUN_DIR"' EXIT

fetch() {  # version sha256 -> prints the sdist root
    local version="$1" sha="$2" dir="$RUN_DIR/$1" tarball="$SRC/django-$1.tar.gz"
    if [[ ! -f "$tarball" ]]; then
        # `|| return 1` throughout: set -e doesn't apply inside $(fetch ...).
        curl -fsSL -o "$tarball.part.$$" "https://files.pythonhosted.org/packages/source/d/django/django-$version.tar.gz" \
            || { rm -f "$tarball.part.$$"; echo "could not download django-$version.tar.gz" >&2; return 1; }
        mv "$tarball.part.$$" "$tarball" || return 1
    fi
    if [[ "$(shasum -a 256 "$tarball" | cut -d' ' -f1)" != "$sha" ]]; then
        echo "django-$version.tar.gz does not match its sha256; deleted it" >&2
        rm -f "$tarball"
        return 1
    fi
    mkdir -p "$dir" || return 1
    tar xzf "$tarball" -C "$dir" || return 1
    echo "$dir/django-$version"
}

failed=()
ran=0
for row in "${VERSIONS[@]}"; do
    read -r line version sha newest <<<"$row"
    if (( ${#lines[@]} )); then
        [[ " ${lines[*]} " == *" $line "* ]] || continue
    else
        # Lines are only run on Pythons they support (3.15 has 6.1 only).
        [[ "$(printf '%s\n%s\n' "$pyver" "$newest" | sort -V | tail -1)" == "$newest" ]] || continue
    fi
    root="$(fetch "$version" "$sha")"
    printf '\n==> Django %s on %s (%s)\n' "$version" "$pylabel" "$env"
    ran=$((ran + 1))
    if ! "$python" -I django_suite/run.py "$root" "$version" test oxide_suite --noinput -v 1; then
        failed+=("$version")
    fi
done

if (( ran == 0 )); then
    echo "no Django line selected for Python $pyver" >&2
    exit 2
fi
if (( ${#failed[@]} )); then
    echo "Django suite failed on: ${failed[*]}" >&2
    exit 1
fi
echo "Django suite passed ($ran line(s), Python $pylabel, $env)"
