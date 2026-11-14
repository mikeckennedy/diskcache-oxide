#!/usr/bin/env bash
# The shuttle model checker for the fork gate, the write lock (plans/06, step 7) and background
# maintenance against the last close (plan 28).
#
# Usage: scripts/model-check.sh [iterations]     (default 100000 schedules per scheduler)
#
# Builds core with `--cfg shuttle`, which swaps the gate's and genlock's atomics, thread-locals,
# sleeps and clock for shuttle's (src/sync.rs), then runs the models (src/gate/model.rs,
# src/genlock/model.rs, src/background/model.rs) under a random scheduler and PCT at depths 2 and 3. A failure prints the
# schedule; DCO_MODEL_REPLAY=<schedule> with a test-name filter replays it. Its own target dir, so
# the cfg never invalidates the normal build. DCO_MODEL_SECONDS caps each model's time if set.
set -euo pipefail
cd "$(dirname "$0")/.."

export RUSTFLAGS="${RUSTFLAGS:-} --cfg shuttle"
export CARGO_TARGET_DIR="$PWD/target/shuttle"
export DCO_MODEL_ITERATIONS="${1:-${DCO_MODEL_ITERATIONS:-100000}}"

cargo clippy -p diskcache-oxide-core --all-targets --quiet -- -D warnings
# Only the models: under the cfg, the other unit tests would panic (shuttle's atomics work only
# inside a shuttle execution). DCO_MODEL_REPLAY=<schedule> replays a failure; filter to its model.
cargo test -p diskcache-oxide-core --profile model-check --lib --quiet -- model::
