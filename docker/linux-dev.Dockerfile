# Linux test image for diskcache-oxide: Ubuntu 26.04 LTS (Talk Python's production OS) with the
# Rust toolchain from rust-toolchain.toml, cargo-deny, uv and every Python the checks run on.
#
# Built and run by scripts/linux-check.sh; the image holds toolchains only, never the repo.
#   docker build -f docker/linux-dev.Dockerfile -t dco-linux-dev docker/
FROM ubuntu:26.04

ARG RUST_TOOLCHAIN=1.99.1
ARG CARGO_DENY_VERSION=0.20.2
ARG PYTHONS="3.14 3.14t 3.15 3.15t"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    RUSTUP_HOME=/opt/rustup \
    CARGO_HOME=/opt/cargo \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_LINK_MODE=copy \
    UV_CACHE_DIR=/cache/uv \
    CARGO_TARGET_DIR=/cache/target \
    PATH=/opt/cargo/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential curl git ca-certificates procps tzdata gdb \
    && rm -rf /var/lib/apt/lists/*

# rustup, pinned to the repo's toolchain so rust-toolchain.toml never triggers a download at run time.
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --no-modify-path --profile minimal --default-toolchain "$RUST_TOOLCHAIN" \
            --component rustfmt --component clippy \
    && rustc --version && cargo fmt --version && cargo clippy --version

# cargo-deny: the prebuilt musl binary (a source build costs minutes). Stripping the archive's top
# directory keeps tar from creating one: under OrbStack's Rosetta (--platform linux/amd64 on an
# arm64 Mac), GNU tar fails every file it extracts into a directory it made ("Cannot open:
# Function not implemented").
RUN arch="$(uname -m)" \
    && mkdir /tmp/cargo-deny \
    && curl -sSfL "https://github.com/EmbarkStudios/cargo-deny/releases/download/${CARGO_DENY_VERSION}/cargo-deny-${CARGO_DENY_VERSION}-${arch}-unknown-linux-musl.tar.gz" \
        | tar -xz -C /tmp/cargo-deny --strip-components=1 \
    && install -m 0755 /tmp/cargo-deny/cargo-deny /opt/cargo/bin/ \
    && rm -rf /tmp/cargo-deny \
    && cargo deny --version

# uv (official installer) and uv-managed Pythons.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh \
    && uv --version \
    && uv python install $PYTHONS \
    && uv python list --only-installed

# Run as the image's unprivileged `ubuntu` user (uid 1000): as root, the permission tests would pass
# for the wrong reason. The cache dirs exist here so the named volumes mounted over them inherit
# this ownership when Docker first creates them.
RUN mkdir -p /cache/target /cache/uv /opt/cargo/registry /opt/cargo/git /work \
    && chown -R ubuntu:ubuntu /cache /work /opt/cargo /opt/rustup /opt/uv-python
USER ubuntu
WORKDIR /work
