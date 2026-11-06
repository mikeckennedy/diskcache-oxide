# Plan 12 step 5, build-order step 4 (the measurement table and M2) inside the dco-linux-dev container.
# `build` makes release wheels from /before and /after (clean exports or worktrees of the two commits)
# into the cache volume, once; `probe` runs bench/scaling/m2_probe.py on them, `fanout` runs
# bench/scaling/fanout_ab.py (bench/fanout.py, interleaved by build), and `hold` runs one probe case
# continuously on the after build, and `profile` records it with perf and writes the 5c wait shares
# (needs --privileged --user root; installs linux-perf into the throwaway container if it is missing).
# Output files go to /out.
# Usage (from the repo root; the two worktrees at commits 721de5a and 6bfe945):
#   docker run --rm --cpus 8 -v "$PWD":/src:ro -v BEFORE:/before:ro -v AFTER:/after:ro \
#       -v cachebench-cache:/cache dco-linux-dev bash /src/bench/scaling/linux_m2.sh build 3.14t
#   docker run --rm [--cpus 4] -v "$PWD":/src:ro -v cachebench-cache:/cache -v OUTDIR:/out \
#       dco-linux-dev bash /src/bench/scaling/linux_m2.sh probe 3.14t NAME [m2_probe.py args...]
#   ... linux_m2.sh fanout 3.14t NAME [fanout_ab.py args...]
#   docker run --rm --cpus 8 --privileged --user root ... linux_m2.sh profile 3.14t NAME
set -euo pipefail
WHAT="$1"; PY="$2"; shift 2
WHEELS=/cache/wheel-m2
if [[ "$WHAT" == "build" ]]; then
    # after-sym: the after commit with symbols kept (strip off; same code and optimization), for perf.
    for arm in before after after-sym; do
        src=/"${arm%-sym}"
        strip=symbols
        [[ "$arm" == *-sym ]] && strip=none
        rm -rf /work/"$arm" && mkdir -p /work/"$arm" && cd "$src"
        tar --exclude=./target --exclude='./.venv*' --exclude=./venv --exclude=./.git --exclude=./dist \
            --exclude=./site --exclude=./soak-results -cf - . | tar -C /work/"$arm" -xf -
        cd /work/"$arm"
        rm -rf "$WHEELS-$arm" && mkdir -p "$WHEELS-$arm"
        CARGO_PROFILE_RELEASE_STRIP="$strip" CARGO_TARGET_DIR=/cache/target-m2-"$arm" \
            uvx --from 'maturin>=1.15,<2' maturin build --release --quiet --interpreter "$PY" --out "$WHEELS-$arm" 2>&1 | tail -1
    done
    exit 0
fi
NAME="$1"; shift
if [[ "$WHAT" == "profile" ]]; then
    command -v perf >/dev/null || { apt-get update -qq >/dev/null &&
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq linux-perf >/dev/null; }
fi
for arm in before after; do
    uv venv --quiet --python "$PY" /work/venv-"$arm"
    uv pip install --quiet --python /work/venv-"$arm"/bin/python "$WHEELS-$arm"/*.whl 'diskcache==5.6.3'
done
mkdir -p /work/tmp /out
echo "== $(nproc) cpus, cpu.max $(cat /sys/fs/cgroup/cpu.max 2>/dev/null), $(uname -sr), load $(cut -d' ' -f1-3 /proc/loadavg)"
ARMS=(--arm before=/work/venv-before/bin/python --arm after=/work/venv-after/bin/python)
case "$WHAT" in
    probe) /work/venv-after/bin/python /src/bench/scaling/m2_probe.py /work/tmp "${ARMS[@]}" --json /out/"$NAME".json "$@" ;;
    fanout) /work/venv-after/bin/python /src/bench/scaling/fanout_ab.py /out/"$NAME" "${ARMS[@]}" "$@" ;;
    hold) /work/venv-after/bin/python /src/bench/scaling/m2_probe.py /work/tmp "$@" ;;
    profile)
        # On-CPU (cpu-clock) and off-CPU (sched_switch) stacks, 4 threads on one handle, after build with symbols.
        uv venv --quiet --python "$PY" /work/venv-sym
        uv pip install --quiet --python /work/venv-sym/bin/python "$WHEELS"-after-sym/*.whl
        for case in 'cache 4t' 'fanout8 4t'; do
            for ev in cpu-clock sched:sched_switch; do
                tag="$NAME-${case// /-}-${ev//:/_}"
                /work/venv-sym/bin/python /src/bench/scaling/m2_probe.py /work/tmp --hold "$case" --seconds 14 &
                pid=$!
                sleep 4
                perf record -q -e "$ev" -g -p "$pid" -o /work/"$tag".data -- sleep 6 || true
                wait "$pid"
                extra=()
                [[ "$ev" == sched* ]] && extra=(--all-waits)
                perf script -i /work/"$tag".data 2>/dev/null > /work/"$tag".script
                /work/venv-after/bin/python /src/bench/scaling/wait_share.py --perf /work/"$tag".script "${extra[@]}" \
                    | tee /out/"$tag".txt
                gzip -c /work/"$tag".script > /out/"$tag".script.gz
            done
        done ;;
    *) echo "unknown: $WHAT" >&2; exit 2 ;;
esac
