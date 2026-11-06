# The adaptive-spin sweep (plan 12 step 5a-2) inside the dco-linux-dev container. `build` makes one
# `--cfg dco_spin_sweep` release wheel from /src into the cache volume (once, at a generous quota);
# `run` installs it and runs bench/scaling/spin_sweep.py, so each quota's run measures the same build.
# The adaptive arms need bench/results/adaptive-spin-2026-11-05.diff applied before `build`.
# Usage (from the repo root):
#   docker run --rm --cpus 8 -v "$PWD":/src:ro -v cachebench-cache:/cache \
#       dco-linux-dev bash /src/bench/scaling/linux_adaptive_spin.sh build 3.14t
#   docker run --rm --cpus 4 -v "$PWD":/src:ro -v cachebench-cache:/cache \
#       dco-linux-dev bash /src/bench/scaling/linux_adaptive_spin.sh run 3.14t [spin_sweep.py args...]
set -euo pipefail
WHAT="$1"; PY="$2"; shift 2
WHEELS=/cache/wheel-adaptive-spin
if [[ "$WHAT" == "build" ]]; then
    rm -rf /work/src && mkdir -p /work/src && cd /src
    tar --exclude=./target --exclude='./.venv*' --exclude=./venv --exclude=./.git --exclude=./dist --exclude=./site \
        --exclude=./soak-results -cf - . | tar -C /work/src -xf -
    cd /work/src
    rm -rf "$WHEELS" && mkdir -p "$WHEELS"
    RUSTFLAGS="--cfg dco_spin_sweep" CARGO_TARGET_DIR=/cache/target-adaptive-spin \
        uvx --from 'maturin>=1.15,<2' maturin build --release --quiet --interpreter "$PY" --out "$WHEELS" 2>&1 | tail -1
    exit 0
fi
uv venv --quiet --python "$PY" /work/venv
uv pip install --quiet --python /work/venv/bin/python "$WHEELS"/*.whl
mkdir -p /work/tmp
echo "== $(nproc) cpus, $(cat /sys/fs/cgroup/cpu.max 2>/dev/null), $(uname -sr)"
/work/venv/bin/python /src/bench/scaling/spin_sweep.py /work/tmp "$@"
