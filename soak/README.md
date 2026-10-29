# Soak test

A long run (24 hours by default) of many processes and threads against two shared caches, with chaos: SIGKILLed and respawned workers, forks under load, and `clear()`/`expire()`/`cull()` under load. It looks for deadlocks (stalls), races (lost, doubled or corrupt writes, expired values returned, totals drift, missing files), and leaks (RSS, fds, database and WAL growth). Workers alternate between Python 3.14 and 3.14t on the same cache files.

## Setup on a fresh Mac

```sh
xcode-select --install                                                     # C compiler, for the bundled SQLite
curl https://sh.rustup.rs -sSf | sh -s -- -y --profile minimal             # Rust (rust-toolchain.toml picks the version)
curl -LsSf https://astral.sh/uv/install.sh | sh                            # uv (it downloads Python 3.14 and 3.14t itself)
exec $SHELL -l                                                             # pick up the new PATH
```

## Run

```sh
tar xzf diskcache-oxide-soak-*.tar.gz && cd diskcache-oxide-soak-*/
nohup soak/run.sh --hours 24 > soak.out 2>&1 &                             # survives closing the terminal or ssh
tail -F soak-results/latest/status.txt                                     # live status, updated every minute
```

`soak/run.sh` builds both Pythons (the first build takes a few minutes), checks the build, and keeps the Mac awake with `caffeinate`. Options pass through to `python -m soak.soak` (`--help` lists them): for example `--minutes 10` for a quick trial, or `--procs 8 --threads 4`. Stop early with `kill -INT <pid of soak.soak>`; it still runs the final checks and writes the summary.

## What it records

All in `soak-results/<timestamp>/` (`latest` points to the newest):

- `status.txt`: live status, rewritten every interval.
- `report.html`: charts and the verdict. It refreshes itself every minute while the run is going; open it in a browser.
- `summary.md`: the final verdict, leak slopes and problem list, written at the end. `python -m soak.report <dir>` rebuilds it and the report from the data at any time.
- `timeline.csv`: the whole system, one row per interval.
- `workers.csv`: each worker, one row per interval.
- `maint.csv`: every maintenance call.
- `events.jsonl`: every event; problems have `"problem": true`.
- `stalls/`: native stack samples (`sample`) captured when a worker or maintenance call stops making progress. The matching Python tracebacks are in `logs/<worker>.log`.

The verdict is PASS (no problems, no flags), WARN (no problems, but a leak, latency-drift or WAL flag), or FAIL.
