#!/usr/bin/env bash
# Plan 33, step 2: does a plain Cache read slower with more processes on x86-64 Linux, as it does on macOS?
# Runs the bare fcntl lock probe on the host's own python3 (standard library only), then the probe again and
# bench/read_procs.py in a throwaway container with the released wheel from PyPI. Nothing is installed on the
# host, the repo is mounted read-only, and everything written lands in OUTDIR.
#
# Run it on a quiet host. The probe takes about 2 minutes. The table takes about 15 minutes for each Python and
# keeps up to RP_COUNTS's largest number of CPUs busy for that long, so not while production traffic is high.
#
# Usage, from a clone of the repo:
#   bench/scaling/htz_read_procs.sh [OUTDIR]              # default OUTDIR: ./read-procs-htz-<date>
#   RP_PROBE_ONLY=1 bench/scaling/htz_read_procs.sh       # the lock probe alone: about 2 minutes, no Docker
#   RP_PYTHONS="3.14" RP_COUNTS=1,2,3,4 RP_ROUNDS=3 bench/scaling/htz_read_procs.sh
#   RP_ONLY="oxide Cache|raw sqlite3" ...                 # only variants whose names start with one of these
#
# Produces in OUTDIR: host.txt, fcntl-host.{md,json}, fcntl-container.{md,json} and
# read-procs-<python>-<machine>.{md,json}. Send the directory back.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${1:-$ROOT/read-procs-htz-$(date +%Y%m%d)}"
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
COUNTS="${RP_COUNTS:-1,2,3,4,8}"
ROUNDS="${RP_ROUNDS:-5}"
PYTHONS="${RP_PYTHONS:-3.14 3.14t}"
MACHINE="${RP_MACHINE:-htz}"
OXIDE="${RP_OXIDE:-0.5.8}"
IMAGE="${RP_IMAGE:-ghcr.io/astral-sh/uv:python3.14-bookworm-slim}"

{
    uname -a
    nproc
    lscpu | grep -E 'Model name|^CPU\(s\)|Thread|Core' || true
    df -T "$OUT" | tail -1
    echo "load before: $(uptime)"
} > "$OUT/host.txt"

python3 "$ROOT/bench/scaling/fcntl_probe.py" --counts "$COUNTS" --rounds "$ROUNDS" --json "$OUT/fcntl-host.json" |
    tee "$OUT/fcntl-host.md"
[[ -n "${RP_PROBE_ONLY:-}" ]] && { echo "done: $OUT"; exit 0; }

# Scratch caches go under OUTDIR, on the host's own filesystem, not the container's overlay.
mkdir -p "$OUT/tmp" "$OUT/uv-cache"
RUN=(docker run --rm -v "$ROOT":/src:ro -v "$OUT":/out -w /src -e TMPDIR=/out/tmp -e UV_CACHE_DIR=/out/uv-cache
    -e UV_PYTHON_INSTALL_DIR=/out/uv-cache/python -e PYTHONDONTWRITEBYTECODE=1 "$IMAGE")
"${RUN[@]}" python bench/scaling/fcntl_probe.py --counts "$COUNTS" --rounds "$ROUNDS" --json /out/fcntl-container.json |
    tee "$OUT/fcntl-container.md"

ONLY=()
if [[ -n "${RP_ONLY:-}" ]]; then
    IFS='|' read -ra NAMES <<< "$RP_ONLY"
    for name in "${NAMES[@]}"; do ONLY+=(--only "$name"); done
fi
for py in $PYTHONS; do
    echo "load before $py: $(uptime)" | tee -a "$OUT/host.txt"
    "${RUN[@]}" uv run --no-project --python "$py" --with "diskcache-oxide==$OXIDE" --with diskcache==5.6.3 \
        python -m bench.read_procs --machine "$MACHINE" --counts "$COUNTS" --rounds "$ROUNDS" --out /out ${ONLY[@]+"${ONLY[@]}"}
done
rm -rf "$OUT/tmp" "$OUT/uv-cache"
echo "done: $OUT"
