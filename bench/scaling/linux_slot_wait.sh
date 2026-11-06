# The slot-wait before/after probe (plan 12 step 5, 5b) inside the dco-linux-dev container. Builds
# three release wheels for one Python: "before" from /before (an export of the commit before 5b),
# "after" and a `--cfg dco_spin_sweep` build from /src, then runs bench/scaling/slot_wait.py, or
# bench/scaling/slot_tail.py with --tail. Usage (from the repo root):
#   git archive <before-commit> | tar -x -C /some/dir
#   docker run --rm --cpus 8 -v "$PWD":/src:ro -v /some/dir:/before:ro -v cachebench-cache:/cache \
#       dco-linux-dev bash /src/bench/scaling/linux_slot_wait.sh 3.14t [--tail] [slot_wait.py args...]
set -euo pipefail
PY="$1"; shift
build() { # name source-dir rustflags
    rm -rf /work/"$1" && mkdir -p /work/"$1" && cd "$2"
    tar --exclude=./target --exclude='./.venv*' --exclude=./venv --exclude=./.git --exclude=./dist --exclude=./site \
        --exclude=./soak-results -cf - . | tar -C /work/"$1" -xf -
    cd /work/"$1"
    RUSTFLAGS="$3" CARGO_TARGET_DIR=/cache/target-slot-"$1" \
        uvx --from 'maturin>=1.15,<2' maturin build --release --quiet --interpreter "$PY" --out /work/wheel-"$1" 2>&1 | tail -1
    uv venv --quiet --python "$PY" /work/venv-"$1"
    uv pip install --quiet --python /work/venv-"$1"/bin/python /work/wheel-"$1"/*.whl
}
build before /before ""
build after /src ""
build sweep /src "--cfg dco_spin_sweep"
mkdir -p /work/tmp
echo "== $(nproc) cpus, $(uname -sr)"
if [[ "${1:-}" == "--tail" ]]; then
    shift
    for arm in before after; do
        echo "== tail $arm"
        /work/venv-"$arm"/bin/python /src/bench/scaling/slot_tail.py /work/tmp "$@"
    done
    exit 0
fi
# "$@" may name more arms; the sweep build's Python is /work/venv-sweep/bin/python.
/work/venv-after/bin/python /src/bench/scaling/slot_wait.py /work/tmp \
    --arm before=/work/venv-before/bin/python --arm after=/work/venv-after/bin/python "$@"
