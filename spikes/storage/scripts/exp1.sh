#!/usr/bin/env bash
# Experiment 1 matrix. Each line of output is one run; summarize with scripts/summarize_exp1.sh.
set -euo pipefail
cd "$(dirname "$0")/.."
R=./target/release/race
D=runs/race
for T in 32768 65536; do
  for i in $(seq 20); do $R run --dir $D --threshold $T --ops 3000 --readers 4; done
  for i in $(seq 5); do $R run --dir $D --threshold $T --ops 3000 --readers 6; done
  for g in 100 1000; do for i in $(seq 5); do $R run --dir $D --threshold $T --ops 3000 --readers 4 --gap-us $g; done; done
  for g in 0 100; do for i in $(seq 5); do $R run --dir $D --threshold $T --ops 3000 --readers 4 --deletes --gap-us $g; done; done
  for i in $(seq 10); do $R run --dir $D --threshold $T --ops 3000 --readers 4 --naive; done
  for i in $(seq 3); do $R run --dir $D --threshold $T --ops 3000 --readers 4 --naive --gap-us 100; done
  for i in $(seq 3); do $R run --dir $D --threshold $T --ops 3000 --readers 4 --naive --naive-delete --gap-us 100; done
done
