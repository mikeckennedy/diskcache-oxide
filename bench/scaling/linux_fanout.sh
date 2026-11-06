# Run bench.fanout inside the dco-linux-dev container: builds a release wheel for each Python it is
# given, then runs the benchmark, writing results to /out. Usage (from the repo root):
#   docker run --rm --cpus 8 -v "$PWD":/src:ro -v cachebench-cache:/cache -v "$PWD/bench/results/linux":/out \
#       dco-linux-dev bash /src/bench/scaling/linux_fanout.sh 3.14t --only small --only threads
set -euo pipefail
PY="$1"; shift
mkdir -p /work/lib && rm -rf /work/lib/* && cd /src
tar --exclude=./target --exclude='./.venv*' --exclude=./venv --exclude=./.git --exclude=./dist --exclude=./site --exclude=./soak-results -cf - . | tar -C /work/lib -xf -
cd /work/lib
rm -rf /work/wheel-"$PY"
uvx --from 'maturin>=1.15,<2' maturin build --release --quiet --interpreter "$PY" --out /work/wheel-"$PY" 2>&1 | tail -1
uv venv --quiet --python "$PY" /work/venv-"$PY"
uv pip install --quiet --python /work/venv-"$PY"/bin/python /work/wheel-"$PY"/*.whl 'diskcache==5.6.3'
echo "== $(nproc) cpus, $(uname -sr)"
/work/venv-"$PY"/bin/python -m bench.fanout --machine linux-vm --out "/out/fanout-$PY-linux-vm.json" "$@"
