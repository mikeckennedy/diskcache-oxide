set -euo pipefail
rm -rf /work/lib && cp -r /src /work/lib && cd /work/lib
uvx --from 'maturin>=1.15,<2' maturin build --release --quiet --interpreter 3.15t --out /work/wheel 2>&1 | tail -1
uv venv --quiet --allow-existing --python 3.15t /work/venv && uv pip install --quiet --reinstall --python /work/venv/bin/python /work/wheel/*.whl
mkdir -p /work/tmp
/work/venv/bin/python /probes/conn_sweep.py /work/tmp
