#!/usr/bin/env bash
# Experiment 4 matrix: writer busy policy x pause between clear chunks. Run alone (timing-sensitive).
set -euo pipefail
S="$(cd "$(dirname "$0")/.." && pwd)"
C=$S/target/release/clearfair
D=$S/runs/clear
for w in default poll; do
  for p in 0 100 250 500 1000 2000; do
    $C run --dir $D --trials 20 --chunk 100 --pause-us $p --writer $w
  done
done
# Chunk size sensitivity (poll writer, 0 and 500 us pause), and a 100 us poll.
for ch in 50 250 1000; do $C run --dir $D --trials 20 --chunk $ch --pause-us 0 --writer poll; $C run --dir $D --trials 20 --chunk $ch --pause-us 500 --writer poll; done
$C run --dir $D --trials 20 --chunk 100 --pause-us 0 --writer poll --poll-us 100
$C run --dir $D --trials 20 --chunk 100 --pause-us 250 --writer poll --poll-us 100
