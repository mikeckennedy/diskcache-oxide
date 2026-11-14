#!/usr/bin/env bash
# Smoke-tests the example app's Docker image and Compose file (examples/webapp): builds the image
# with a local diskcache-oxide wheel, starts the web app and the worker on one named volume, waits
# for /health/cache, loads a page, then removes everything, volume included.
#
# Usage: scripts/example-docker.sh [wheel]
#   default: dist/diskcache_oxide-<version>-cp314-cp314-manylinux_2_28_<arch>.whl for Docker's
#   architecture (scripts/build_linux.sh --arch <arch> 3.14 builds it). Run it at release time; it
#   isn't part of check.sh, which doesn't build images.
set -euo pipefail
cd "$(dirname "$0")/.."

version="$(sed -n 's/^version = "\(.*\)"$/\1/p' Cargo.toml | head -1)"
case "$(docker version --format '{{.Server.Arch}}')" in
    arm64 | aarch64) arch=aarch64 ;;
    amd64 | x86_64) arch=x86_64 ;;
    *) echo "unsupported Docker architecture" >&2; exit 1 ;;
esac
wheel="${1:-dist/diskcache_oxide-$version-cp314-cp314-manylinux_2_28_$arch.whl}"
if [[ ! -f "$wheel" ]]; then
    echo "no wheel at $wheel; build it with: scripts/build_linux.sh --arch $arch 3.14" >&2
    exit 1
fi

app=examples/webapp
project=dco-example-smoke
port="${EXAMPLE_PORT:-18080}"
compose() { EXAMPLE_PORT="$port" docker compose -p "$project" -f "$app/compose.yaml" "$@"; }

cleanup() {
    local status=$?
    if [[ $status -ne 0 ]]; then
        echo "==> failed; the containers' logs:" >&2
        compose logs --no-color --tail 100 >&2 || true
    fi
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    find "$app/wheels" -name '*.whl' -delete
    return "$status"
}
trap cleanup EXIT

cp "$wheel" "$app/wheels/"
echo "==> building the image with $(basename "$wheel")"
compose build --quiet --build-arg LOCAL_WHEELS=1
echo "==> starting the web app and the worker"
compose up -d

for _ in $(seq 1 60); do
    if curl -fsS "http://127.0.0.1:$port/health/cache" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -fsS "http://127.0.0.1:$port/health/cache"
echo
curl -fsS "http://127.0.0.1:$port/" | grep -q 'Free-threaded Python, one year in'
curl -fsS "http://127.0.0.1:$port/" | grep -qE '>1 hit<'  # the second view came from the cache
compose exec -T worker python -c 'import os; assert os.getuid() == 10001, os.getuid()'
# The first view queued the sitemap job; the worker container runs it on the shared volume.
for _ in $(seq 1 30); do
    if compose exec -T worker test -f /var/lib/my-app/public/sitemap.xml; then
        break
    fi
    sleep 1
done
compose exec -T worker test -f /var/lib/my-app/public/sitemap.xml
echo "==> the example app works in Docker"
