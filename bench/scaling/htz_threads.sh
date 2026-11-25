#!/usr/bin/env bash
# Plan 34, step 1 on an x86-64 Linux host such as htz: bench/threads.py with the released wheel from PyPI, in a
# throwaway container. Nothing is installed on the host, the repo is mounted read-only, and everything written lands
# in OUTDIR. Each Python runs as its own invocation, so its rounds interleave the variants but not the interpreters.
#
# Run it on a quiet host: it keeps up to 8 CPUs busy for about 12 minutes per Python.
#
# Usage, from a clone of the repo (or a copy of its bench/ directory):
#   bench/scaling/htz_threads.sh [OUTDIR]                      # default OUTDIR: ./threads-htz-<date>
#   TH_PYTHONS="3.14t" TH_ROUNDS=3 TH_COUNTS=1,2,4 bench/scaling/htz_threads.sh
#   TH_ONLY="oxide Cache, shared|oxide FanoutCache(8), shared" TH_MACHINE=htz-fanout ...   # some variants only
#
# Produces in OUTDIR: host.txt and threads-htz-<python>.{md,json}. Send the directory back.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$ROOT/threads-htz-$(date +%Y%m%d)}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
COUNTS="${TH_COUNTS:-1,2,4,8}"
ROUNDS="${TH_ROUNDS:-5}"
PYTHONS="${TH_PYTHONS:-3.14 3.14t 3.15t}"
OXIDE="${TH_OXIDE:-0.5.8}"
IMAGE="${TH_IMAGE:-ghcr.io/astral-sh/uv:python3.14-bookworm-slim}"
MACHINE="${TH_MACHINE:-htz}"
ONLY=""
if [[ -n "${TH_ONLY:-}" ]]; then
    IFS='|' read -ra NAMES <<< "$TH_ONLY"
    for name in "${NAMES[@]}"; do ONLY+=" --only '$name'"; done
fi

{
    uname -a
    nproc
    lscpu | grep -E 'Model name|^CPU\(s\)|Thread|Core' || true
    echo "load before: $(uptime)"
} > "$OUT/host.txt"

mkdir -p "$OUT/tmp" "$OUT/uv-cache"
RUN=(docker run --rm -v "$ROOT":/src:ro -v "$OUT":/out -w /src -e TMPDIR=/out/tmp -e UV_CACHE_DIR=/out/uv-cache
    -e UV_PYTHON_INSTALL_DIR=/out/uv-cache/python -e PYTHONDONTWRITEBYTECODE=1 "$IMAGE")
for py in $PYTHONS; do
    echo "load before $py: $(uptime)" | tee -a "$OUT/host.txt"
    "${RUN[@]}" uv run --no-project --python "$py" --with "diskcache-oxide==$OXIDE" --with diskcache==5.6.3 \
        sh -c "python -m bench.threads --arm $py=\$(command -v python) --machine $MACHINE-$py --counts $COUNTS --rounds $ROUNDS --out /out$ONLY"
done
rm -rf "$OUT/tmp" "$OUT/uv-cache"
echo "done: $OUT"
