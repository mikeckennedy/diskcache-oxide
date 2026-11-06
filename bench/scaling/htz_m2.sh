#!/usr/bin/env bash
# The plan 12 step 5 measurement (build-order step 4, the M2 table) on an x86-64 Linux host such as
# htz, so its rows sit beside the Mac's and the arm64 VM's in bench/results/m2-measurement-2026-11-05.md.
# Everything runs in Docker (the repo's dco-linux-dev image); nothing is installed on the host and the
# repo is mounted read-only. Run it on a quiet host: it keeps 8 (then 4) CPUs busy for about an hour
# in all, so not while production traffic is high.
#
# Usage, from a clone of the repo at 6bfe945 or later:
#   bench/scaling/htz_m2.sh [OUTDIR]        # default OUTDIR: ./m2-htz-<date>
#   M2_SKIP_FANOUT=1 bench/scaling/htz_m2.sh   # the probe and profile only (about 10 minutes)
#   M2_PROFILE=1 bench/scaling/htz_m2.sh       # also the perf profile (opt-in: it needs a --privileged container)
#
# Produces in OUTDIR: probe-cpus8.txt / probe-cpus4.txt (the before/after table, 7 rounds), fanout.txt
# (bench/fanout.py before/after, 5 rounds), and, with M2_PROFILE=1, perf-*.txt (the 5c wait shares). Send the directory back.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$(mkdir -p "${1:-$ROOT/m2-htz-$(date +%Y%m%d)}" && cd "${1:-$ROOT/m2-htz-$(date +%Y%m%d)}" && pwd)"
WT="$(mktemp -d)"
trap 'git -C "$ROOT" worktree remove --force "$WT/before" 2>/dev/null; git -C "$ROOT" worktree remove --force "$WT/after" 2>/dev/null; rm -rf "$WT"' EXIT

cd "$ROOT"
git worktree add --detach "$WT/before" 721de5a >/dev/null # the last commit before plan 12 step 5
git worktree add --detach "$WT/after" 6bfe945 >/dev/null  # 5a's fixed spin and 5b's slot-wait spin
docker image inspect dco-linux-dev >/dev/null 2>&1 ||
    docker build -f docker/linux-dev.Dockerfile -t dco-linux-dev docker
{ uname -a; nproc; lscpu | grep -E 'Model name|^CPU\(s\)|Thread|Core'; uptime; } > "$OUT/host.txt"

RUN=(docker run --rm -v "$ROOT":/src:ro -v cachebench-cache:/cache -v "$OUT":/out)
"${RUN[@]}" --cpus 8 -v "$WT/before":/before:ro -v "$WT/after":/after:ro dco-linux-dev \
    bash /src/bench/scaling/linux_m2.sh build 3.14t
for c in 8 4; do
    echo "load before --cpus $c: $(uptime)" | tee -a "$OUT/host.txt"
    "${RUN[@]}" --cpus "$c" dco-linux-dev bash /src/bench/scaling/linux_m2.sh probe 3.14t "probe-cpus$c" --rounds 7 \
        | tee "$OUT/probe-cpus$c.txt"
done

# 5c: on-CPU (cpu-clock) and off-CPU (sched_switch) stacks of the after build, 4 threads on one handle.
# perf runs as root in a privileged throwaway container (linux-perf is added to it); the host is untouched.
# Off unless M2_PROFILE=1 (a privileged container on a production host is your call).
if [[ -n "${M2_PROFILE:-}" ]]; then
    "${RUN[@]}" --cpus 8 --privileged --user root dco-linux-dev bash /src/bench/scaling/linux_m2.sh profile 3.14t perf
fi

if [[ -z "${M2_SKIP_FANOUT:-}" ]]; then
    "${RUN[@]}" --cpus 8 dco-linux-dev bash /src/bench/scaling/linux_m2.sh fanout 3.14t fanout --rounds 5 \
        > "$OUT/fanout.txt" 2>&1
    tail -45 "$OUT/fanout.txt"
fi
echo "done: $OUT"
