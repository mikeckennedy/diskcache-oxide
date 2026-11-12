#!/usr/bin/env bash
# Which release gate a change needs (CLAUDE.md, "Release tiers"), from the files changed since a base.
#
# Usage: scripts/release_tier.sh [base]     base defaults to the newest v* tag; head is HEAD
#
# Prints the tier (none, patch or full) and each file that set it. The tier is a floor, not
# a ceiling: a patch-tier change that touches concurrency in a way this list can't see still gets
# the full gate. A "format?" line means the change may alter what's on disk: if it does, the
# release is a format change (a minor bump, the full gate and upgrade notes); if it doesn't, the
# line is advice only.
set -euo pipefail
cd "$(dirname "$0")/.."

base=${1:-$(git tag -l 'v*' --sort=-version:refname | head -1)}
[[ -n "$base" ]] || { echo "no v* tag yet: pass the last release's commit, e.g. scripts/release_tier.sh 8f98065" >&2; exit 2; }
git rev-parse -q --verify "$base^{commit}" >/dev/null || { echo "unknown base: $base" >&2; exit 2; }

core=crates/diskcache-oxide-core/src
# The areas the long soak and the sanitizers exist for: locks, the fork gate, our SQLite mutexes,
# the lock-free stack, connections, the WAL, value files, the row writer, eviction and maintenance.
full_re="^($core/(gate|genlock|sync|model|stack|sqlite_mutex|sqlite|lockwait|poison|pool|store|open|wal|spill|rows|ops|maintenance)(\.rs|/)|crates/diskcache-oxide-core/tests/|\.cargo/config\.toml$|rust-toolchain\.toml$)"
# What's written to disk: the schema, key encoding, the fanout root, the value codecs.
format_re="^($core/(schema|key|fanout)\.rs$|crates/diskcache-oxide-py/src/codec/)"
# Shipped code that isn't in either list above.
code_re="^(crates/|python/diskcache_oxide/.*\.py$|Cargo\.toml$|pyproject\.toml$)"

tier=none
rank() { case $1 in none) echo 0;; patch) echo 1;; full) echo 2;; esac; }
raise() { (( $(rank "$1") > $(rank "$tier") )) && tier=$1 || true; }

lines=()
while IFS= read -r f; do
    if [[ $f =~ $full_re ]]; then raise full; lines+=("full     $f")
    elif [[ $f =~ $format_re ]]; then raise patch; lines+=("format?  $f")
    elif [[ $f =~ $code_re ]]; then raise patch; lines+=("patch    $f")
    fi
done < <(git diff --name-only "$base" HEAD)

# A new SQLite, rusqlite or PyO3 is a full-gate change even though it's one line in Cargo.lock.
if git diff -U2 "$base" HEAD -- Cargo.lock | grep -B2 '^[-+]version' \
        | grep -qE 'name = "(libsqlite3-sys|rusqlite|pyo3[a-z-]*)"'; then
    raise full; lines+=("full     Cargo.lock (libsqlite3-sys, rusqlite or pyo3 changed)")
fi

echo "changes since $base ($(git rev-parse --short "$base")..$(git rev-parse --short HEAD))"
((${#lines[@]})) && printf '  %s\n' "${lines[@]}"
echo "tier: $tier"
case $tier in
    none)   echo "  no release needed for these changes" ;;
    patch)  echo "  gate: scripts/check.sh, scripts/sanitize.sh --quick, a 2-hour soak; then the 24-hour soak on the released commit" ;;
    full)   echo "  gate: scripts/sanitize.sh (every lane), a 24-hour soak on the final commit, before the release" ;;
esac
