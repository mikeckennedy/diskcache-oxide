#!/usr/bin/env bash
# Linux PGO release wheels into dist/, from a Mac. Usage: scripts/build_linux.sh [--arch aarch64,x86_64] [python ...]
#
# Runs scripts/build_pgo.sh unchanged inside the manylinux_2_28 image for each architecture (default:
# both), so the wheels get the manylinux_2_28 tag and link against glibc 2.28. aarch64 runs natively;
# x86_64 runs under Rosetta (OrbStack or Docker Desktop with Rosetta on). Emulation makes the x86_64
# build slower but not different: PGO counts what ran, not how long it took. The image ships every
# CPython we target in /opt/python and uv; the pinned Rust toolchain is installed on first run and kept,
# with uv's cache and managed Pythons, in a docker volume per architecture (dco-home-<arch>).
set -euo pipefail
cd "$(dirname "$0")/.."

arches=(aarch64 x86_64)
if [[ ${1:-} == --arch ]]; then
    IFS=, read -ra arches <<< "${2:?--arch needs a value}"
    shift 2
fi

for arch in "${arches[@]}"; do
    case $arch in
        aarch64) platform=linux/arm64 ;;
        x86_64) platform=linux/amd64 ;;
        *) echo "unknown arch: $arch (want aarch64 or x86_64)" >&2; exit 2 ;;
    esac
    printf '\n######## linux/%s ########\n' "$arch"
    docker run --rm --platform "$platform" \
        -v "$PWD:/src" -w /src \
        -v "dco-home-$arch:/root" \
        "quay.io/pypa/manylinux_2_28_$arch" \
        bash -euo pipefail -c '
            export PATH="/root/.cargo/bin:$(printf "%s:" /opt/python/*/bin)$PATH"
            command -v rustup >/dev/null ||
                curl -sSf https://sh.rustup.rs | sh -s -- -y --quiet --profile minimal --default-toolchain none --no-modify-path
            rustup toolchain install >/dev/null   # the channel in rust-toolchain.toml
            nice -n 20 scripts/build_pgo.sh "$@"
        ' -- "$@"
done
