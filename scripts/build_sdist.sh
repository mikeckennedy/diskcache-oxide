#!/usr/bin/env bash
# The source distribution into dist/, checked: what must be in it is there, and nothing that must stay
# out got in. Run by `make sdist`, which `make wheels` includes, so a release's dist/ has the sdist
# beside the wheels.
#
#   scripts/build_sdist.sh [out dir]      # default: dist
set -euo pipefail
cd "$(dirname "$0")/.."

out="${1:-dist}"
version="$(sed -n '/^\[workspace\.package\]/,/^\[/s/^version = "\(.*\)"/\1/p' Cargo.toml)"
[[ -n "$version" ]] || { echo "no workspace.package.version in Cargo.toml" >&2; exit 1; }
sdist="$out/diskcache_oxide-$version.tar.gz"

mkdir -p "$out"
rm -f "$sdist"
# COPYFILE_DISABLE keeps macOS's ._* AppleDouble files out of the tarball.
COPYFILE_DISABLE=1 uvx --from 'maturin>=1.15,<2' maturin sdist --out "$out" >/dev/null  # errors still show
[[ -f "$sdist" ]] || { echo "maturin didn't write $sdist" >&2; exit 1; }

# Listed into a variable first, so a failed tar stops the script under set -e.
listing="$(tar tzf "$sdist")"
root="diskcache_oxide-$version"

# Without .cargo/config.toml a source build silently loses SQLite's compile flags; without Cargo.lock
# it isn't the build we tested.
for needed in PKG-INFO LICENSE Cargo.lock .cargo/config.toml pyproject.toml; do
    if ! grep -qxF "$root/$needed" <<<"$listing"; then
        echo "$sdist has no $needed" >&2
        exit 1
    fi
done

# Top-level directories only (a crate's own tests/ is fine), plus build output and AppleDouble files.
if leaked="$(grep -E "^$root/(examples|django_suite|tests|bench)/|\.(so|pyd|dylib|profraw|profdata)$|/\._" <<<"$listing")"; then
    echo "these ended up in $sdist:" >&2
    echo "$leaked" >&2
    exit 1
fi

packaged="$(tar xzOf "$sdist" "$root/PKG-INFO" | sed -n 's/^Version: //p')"
if [[ "$packaged" != "$version" ]]; then
    echo "$sdist says version $packaged, Cargo.toml says $version" >&2
    exit 1
fi

uvx twine check --strict "$sdist"
echo "sdist: $sdist ($(wc -l <<<"$listing" | tr -d ' ') files)"
