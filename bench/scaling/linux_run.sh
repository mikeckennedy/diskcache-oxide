set -euo pipefail
cp -r /src /work/lib && cd /work/lib
uvx --from 'maturin>=1.15,<2' maturin build --release --quiet --interpreter 3.15t --out /work/wheel 2>&1 | tail -1
uv venv --quiet --python 3.15t /work/venv && uv pip install --quiet --python /work/venv/bin/python /work/wheel/*.whl
mkdir -p /work/tmp
echo "== oxide (Linux, $(nproc) cpus)"; /work/venv/bin/python /probes/scale_probe.py /work/tmp
echo "== plain sqlite3"; /work/venv/bin/python /probes/raw_sqlite.py /work/tmp
/work/venv/bin/python /probes/raw_sqlite.py /work/tmp
