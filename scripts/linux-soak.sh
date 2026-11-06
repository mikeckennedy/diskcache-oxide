#!/usr/bin/env bash
# The soak (soak/run.sh) on Linux: Ubuntu 26.04 LTS in Docker (OrbStack), in the background.
#
# Usage: scripts/linux-soak.sh [--cpus N] [--memory SIZE] [--rebuild] [any soak/run.sh option, e.g. --hours 6]
#
#   --cpus N      CPUs for the container (default 14)
#   --memory SIZE a hard memory cap for the container, e.g. 6g (default none), for a shared host
#   --rebuild     rebuild the dco-linux-dev image even if it exists
#
# Results land on the host in soak-results/linux/<run>/ (soak-results/linux/latest/status.txt to
# follow along), exactly as a macOS run writes soak-results/<run>/. The container is named
# dco-linux-soak and runs detached; `docker logs -f dco-linux-soak` shows the build and the
# per-interval status lines, `docker stop -t 300 dco-linux-soak` ends the run early (the orchestrator
# stops its workers and runs the final checks; plain `docker stop` kills it after 10 s).
#
# Like scripts/linux-check.sh, the repo is mounted read-only and the container builds a copy, so it
# never touches the host's venvs or target dirs; the cargo registry, target dirs and uv cache persist
# in the dco-* named volumes. The image has gdb, and the container may ptrace, so stalls get native
# stacks, as `sample` gives them on macOS.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

IMAGE=dco-linux-dev
NAME=dco-linux-soak
CPUS=14 MEMORY='' REBUILD=0
ARGS=()
while (( $# )); do
    case "$1" in
        --cpus) CPUS="$2"; shift ;;
        --memory) MEMORY="$2"; shift ;;
        --rebuild) REBUILD=1 ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) ARGS+=("$1") ;;
    esac
    shift
done

if (( REBUILD )) || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> building $IMAGE"
    docker build -f "$ROOT/docker/linux-dev.Dockerfile" -t "$IMAGE" "$ROOT/docker"
fi
if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "a container named $NAME already exists (still running?); docker rm -f $NAME to clear it" >&2
    exit 1
fi

OUT="$ROOT/soak-results/linux"
mkdir -p "$OUT"
# The host's time zone, so status.txt and events read in local time, as on macOS.
TZ_NAME="${TZ:-$(readlink /etc/localtime 2>/dev/null | sed 's|.*/zoneinfo/||')}"

# The same snapshot as docker/in-container-check.sh, then soak/run.sh against the copy.
SCRIPT='
set -euo pipefail
command -v gdb >/dev/null || echo "soak: no gdb in the image (scripts/linux-soak.sh --rebuild); stalls get Python tracebacks only"
mkdir -p /work/repo
tar -C /src -cf - \
    --exclude=./target --exclude="./venv" --exclude="./venv*" --exclude="./.venv*" \
    --exclude="*/__pycache__" --exclude=./.pytest_cache \
    --exclude=./.ruff_cache --exclude=./.hypothesis --exclude="./python/diskcache_oxide/*.so" \
    --exclude=./soak-results --exclude=./site \
    . | tar -C /work/repo -xf -
cd /work/repo
printf "%s, %s CPUs, kernel %s\n" "$(. /etc/os-release && echo "$PRETTY_NAME")" "$(nproc)" "$(uname -r)"
export CARGO_TARGET_DIR=/cache/target/soak
# The caches under test on the container disk (soak.py links them into the run dir on /out).
export DCO_SOAK_CACHE_ROOT=/work/caches
exec soak/run.sh --out /out "$@"
'

MEM_ARGS=()
[[ -n "$MEMORY" ]] && MEM_ARGS=(--memory "$MEMORY" --memory-swap "$MEMORY")
docker run -d --rm --init --name "$NAME" --cpus "$CPUS" ${MEM_ARGS[@]+"${MEM_ARGS[@]}"} --shm-size 1g --cap-add SYS_PTRACE -e TZ="${TZ_NAME:-UTC}" \
    -v "$ROOT:/src:ro" \
    -v "$OUT:/out" \
    -v dco-cargo-registry:/opt/cargo/registry \
    -v dco-cargo-git:/opt/cargo/git \
    -v dco-target:/cache/target \
    -v dco-uv-cache:/cache/uv \
    "$IMAGE" bash -c "$SCRIPT" soak ${ARGS[@]+"${ARGS[@]}"} >/dev/null
echo "started $NAME (${CPUS} CPUs): follow with  tail -F $OUT/latest/status.txt  or  docker logs -f $NAME"
