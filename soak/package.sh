#!/usr/bin/env bash
# Packages the working tree (committed and uncommitted, minus anything git ignores) as one tarball
# for the soak machine: dist/diskcache-oxide-soak-<date>-<commit>.tar.gz
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dist
name="diskcache-oxide-soak-$(date +%Y%m%d-%H%M)-$(git rev-parse --short HEAD)"
out="dist/$name.tar.gz"
# BSD tar (macOS) uses -s to prefix paths; GNU tar uses --transform.
if tar --version 2>/dev/null | grep -q 'GNU tar'; then
    git ls-files -co --exclude-standard -z | tar --null -T - --transform "s,^,$name/," -czf "$out"
else
    git ls-files -co --exclude-standard -z | tar --null -T - -s ",^,$name/," -czf "$out"
fi
echo "$out ($(du -h "$out" | cut -f1))"
echo "On the soak machine:"
echo "    tar xzf $(basename "$out") && cd $name && soak/run.sh --hours 24"
