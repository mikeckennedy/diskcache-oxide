#!/usr/bin/env bash
# scripts/check.sh on Linux: Ubuntu 26.04 LTS in Docker (OrbStack), matching Talk Python's servers.
#
# Usage: scripts/linux-check.sh [--slow] [--bench] [--python 3.14,3.14t,...] [--cpus N]
#                               [--no-rust] [--no-tests] [--rounds N] [--exec 'command'] [--rebuild]
#
#   --slow        also run the slow tests (pytest -m slow)
#   --bench       run `python -m bench.gate` on 3.14 and 3.14t; results land in bench/results/linux/
#   --python      which Pythons to test (default 3.14,3.14t,3.15,3.15t)
#   --cpus        CPUs for the container (default 8; benchmarks need at least 8)
#   --no-rust     skip fmt, clippy, cargo test and cargo deny
#   --no-tests    skip the per-Python smoke test, stubtest and pytest (with --bench: bench only)
#   --exec CMD    instead of the checks, run CMD in the repo copy once per Python, with $VENV
#                 (e.g. .venv-ft) and $PY (e.g. 3.14t) set. Example:
#                   scripts/linux-check.sh --python 3.14t --exec '$VENV/bin/pytest -q --backend=oxide -k fork'
#   --rebuild     rebuild the image even if it exists
#
# The repo is mounted read-only; the container copies it and builds the copy, so it never touches
# the host's venvs or target dirs. Cargo's registry, target dirs and uv's cache persist in the named
# volumes dco-cargo-registry, dco-cargo-git, dco-target and dco-uv-cache (`docker volume rm` them to
# start cold). The container is named dco-linux-check (dco-linux-bench with --bench).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

IMAGE=dco-linux-dev
SLOW=0 BENCH=0 RUST=1 TESTS=1 CPUS=8 ROUNDS=4 REBUILD=0 EXEC=''
PYTHONS='3.14,3.14t,3.15,3.15t'
while (( $# )); do
    case "$1" in
        --slow) SLOW=1 ;;
        --bench) BENCH=1 ;;
        --python) PYTHONS="$2"; shift ;;
        --cpus) CPUS="$2"; shift ;;
        --rounds) ROUNDS="$2"; shift ;;
        --no-rust) RUST=0 ;;
        --no-tests) TESTS=0 ;;
        --exec) EXEC="$2"; shift ;;
        --rebuild) REBUILD=1 ;;
        -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

if (( REBUILD )) || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> building $IMAGE"
    docker build -f "$ROOT/docker/linux-dev.Dockerfile" -t "$IMAGE" "$ROOT/docker"
fi

NAME=dco-linux-check
(( BENCH )) && NAME=dco-linux-bench
[[ -n "$EXEC" ]] && NAME=dco-linux-exec
if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "a container named $NAME already exists (still running?); docker rm -f $NAME to clear it" >&2
    exit 1
fi

OUT_ARGS=()
if (( BENCH )); then
    mkdir -p "$ROOT/bench/results/linux"
    OUT_ARGS=(-v "$ROOT/bench/results/linux:/out")
fi

TTY_ARGS=()
[[ -t 1 ]] && TTY_ARGS=(-t)

exec docker run --rm --init ${TTY_ARGS[@]+"${TTY_ARGS[@]}"} --name "$NAME" --cpus "$CPUS" --shm-size 1g \
    -v "$ROOT:/src:ro" \
    -v dco-cargo-registry:/opt/cargo/registry \
    -v dco-cargo-git:/opt/cargo/git \
    -v dco-target:/cache/target \
    -v dco-uv-cache:/cache/uv \
    ${OUT_ARGS[@]+"${OUT_ARGS[@]}"} \
    -e DCO_PYTHONS="$PYTHONS" -e DCO_SLOW="$SLOW" -e DCO_BENCH="$BENCH" -e DCO_RUST="$RUST" \
    -e DCO_TESTS="$TESTS" -e DCO_ROUNDS="$ROUNDS" -e DCO_EXEC="$EXEC" \
    "$IMAGE" bash /src/docker/in-container-check.sh
