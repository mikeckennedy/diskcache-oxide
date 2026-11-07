#!/usr/bin/env bash
# scripts/check.sh on Linux: Ubuntu 26.04 LTS in Docker (OrbStack), matching Talk Python's servers.
#
# Usage: scripts/linux-check.sh [--slow] [--bench] [--python 3.14,3.14t,...] [--cpus N]
#                               [--no-rust] [--no-tests] [--rounds N] [--exec 'command'] [--rebuild]
#                               [--platform linux/amd64] [--out DIR]
#
#   --slow        also run the slow tests (pytest -m slow)
#   --bench       run `python -m bench.gate` on 3.14 and 3.14t; results land in bench/results/linux/
#                 (bench/results/linux-amd64/ with --platform linux/amd64). Refused under emulation.
#   --python      which Pythons to test (default 3.14,3.14t,3.15,3.15t)
#   --cpus        CPUs for the container (default 8; benchmarks need at least 8)
#   --memory      a hard memory cap for the container, e.g. 6g (default none), for a shared host
#   --no-rust     skip fmt, clippy, cargo test and cargo deny
#   --no-tests    skip the per-Python smoke test, ty and pytest (with --bench: bench only)
#   --exec CMD    instead of the checks, run CMD in the repo copy once per Python, with $VENV
#                 (e.g. .venv-ft) and $PY (e.g. 3.14t) set. Example:
#                   scripts/linux-check.sh --python 3.14t --exec '$VENV/bin/pytest -q --backend=oxide -k fork'
#   --rebuild     rebuild the image even if it exists
#   --platform    run another architecture, e.g. linux/amd64 (production) on an arm64 Mac, through
#                 OrbStack's emulation: slow, so judge a timing failure there rather than retry it.
#                 It gets its own image, volumes and container name (suffix -amd64). When the
#                 architecture isn't the host's, DCO_EMULATED is set in the container, and the tests
#                 emulation can't run skip (see docker/in-container-check.sh).
#   --out DIR     mount DIR (created if missing) at /out, for files --exec writes, e.g.
#                   scripts/linux-check.sh --platform linux/amd64 --python 3.14 --out dist/linux \
#                       --exec 'scripts/build_pgo.sh $PY && cp dist/*.whl /out/'
#
# The repo is mounted read-only; the container copies it and builds the copy, so it never touches
# the host's venvs or target dirs. Cargo's registry, target dirs and uv's cache persist in the named
# volumes dco-cargo-registry, dco-cargo-git, dco-target, dco-uv-cache and dco-cpython-san, the last
# holding the sanitizer lanes' CPython builds (plan 14, step 4a) (`docker volume rm` them to
# start cold). The container is named dco-linux-check (dco-linux-bench with --bench, dco-linux-exec
# with --exec). With --platform, the image, volumes and container all take the suffix, e.g. -amd64.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

IMAGE=dco-linux-dev
SLOW=0 BENCH=0 RUST=1 TESTS=1 CPUS=8 MEMORY='' ROUNDS=4 REBUILD=0 EXEC='' PLATFORM='' OUT=''
PYTHONS='3.14,3.14t,3.15,3.15t'
while (( $# )); do
    case "$1" in
        --slow) SLOW=1 ;;
        --bench) BENCH=1 ;;
        --python) PYTHONS="$2"; shift ;;
        --cpus) CPUS="$2"; shift ;;
        --memory) MEMORY="$2"; shift ;;
        --rounds) ROUNDS="$2"; shift ;;
        --no-rust) RUST=0 ;;
        --no-tests) TESTS=0 ;;
        --exec) EXEC="$2"; shift ;;
        --rebuild) REBUILD=1 ;;
        --platform) PLATFORM="$2"; shift ;;
        --out) OUT="$2"; shift ;;
        -h|--help) sed -n '2,33p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

# Another architecture gets its own image, volumes and container name, so its builds never mix
# with the native ones.
SUFFIX='' PLATFORM_ARGS=() EMULATED=0
if [[ -n "$PLATFORM" ]]; then
    arch=${PLATFORM#*/}; arch=${arch%%/*}  # linux/arm64/v8 -> arm64
    SUFFIX="-$arch"
    PLATFORM_ARGS=(--platform "$PLATFORM")
    # Tests that emulation can't run (a failed huge allocation kills the process under Rosetta)
    # skip when DCO_EMULATED is set.
    native=$(uname -m)
    case "$native" in x86_64) native=amd64 ;; aarch64) native=arm64 ;; esac
    [[ "$arch" != "$native" ]] && EMULATED=1
fi
if (( BENCH && EMULATED )); then
    echo "--bench under emulation measures the emulator, not diskcache-oxide; run it natively" >&2
    exit 2
fi
IMAGE="$IMAGE$SUFFIX"

if (( REBUILD )) || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "==> building $IMAGE"
    docker build ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} -f "$ROOT/docker/linux-dev.Dockerfile" -t "$IMAGE" "$ROOT/docker"
fi

NAME=dco-linux-check
(( BENCH )) && NAME=dco-linux-bench
[[ -n "$EXEC" ]] && NAME=dco-linux-exec
NAME="$NAME$SUFFIX"
if docker container inspect "$NAME" >/dev/null 2>&1; then
    echo "a container named $NAME already exists (still running?); docker rm -f $NAME to clear it" >&2
    exit 1
fi

OUT_ARGS=()
if [[ -n "$OUT" ]]; then
    (( BENCH )) && { echo "--out and --bench both use /out; pick one" >&2; exit 2; }
    mkdir -p "$OUT"
    OUT_ARGS=(-v "$(cd "$OUT" && pwd):/out")
elif (( BENCH )); then
    mkdir -p "$ROOT/bench/results/linux$SUFFIX"
    OUT_ARGS=(-v "$ROOT/bench/results/linux$SUFFIX:/out")
fi

TTY_ARGS=()
[[ -t 1 ]] && TTY_ARGS=(-t)
MEM_ARGS=()
[[ -n "$MEMORY" ]] && MEM_ARGS=(--memory "$MEMORY" --memory-swap "$MEMORY")
EMU_ARGS=()
(( EMULATED )) && EMU_ARGS=(-e DCO_EMULATED=1)

exec docker run --rm --init ${PLATFORM_ARGS[@]+"${PLATFORM_ARGS[@]}"} ${TTY_ARGS[@]+"${TTY_ARGS[@]}"} \
    --name "$NAME" --cpus "$CPUS" ${MEM_ARGS[@]+"${MEM_ARGS[@]}"} --shm-size 1g \
    -v "$ROOT:/src:ro" \
    -v "dco-cargo-registry$SUFFIX:/opt/cargo/registry" \
    -v "dco-cargo-git$SUFFIX:/opt/cargo/git" \
    -v "dco-target$SUFFIX:/cache/target" \
    -v "dco-uv-cache$SUFFIX:/cache/uv" \
    -v "dco-cpython-san$SUFFIX:/cache/cpython-san" \
    ${OUT_ARGS[@]+"${OUT_ARGS[@]}"} \
    -e DCO_PYTHONS="$PYTHONS" -e DCO_SLOW="$SLOW" -e DCO_BENCH="$BENCH" -e DCO_RUST="$RUST" \
    -e DCO_TESTS="$TESTS" -e DCO_ROUNDS="$ROUNDS" -e DCO_EXEC="$EXEC" \
    ${EMU_ARGS[@]+"${EMU_ARGS[@]}"} \
    "$IMAGE" bash /src/docker/in-container-check.sh
