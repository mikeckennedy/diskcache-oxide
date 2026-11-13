# diskcache-oxide

A shared, durable disk cache for Python with diskcache's API, a Rust engine and SQLite storage. Safe across threads, processes and `fork()`, including free-threaded Python. API-compatible with diskcache 5.6.3, not data-compatible: our own on-disk format, no pickle unless the caller opts in.

Start here when orienting: [README.md](README.md), [plans/00-roadmap.md](plans/00-roadmap.md) (phases and decisions), [plans/04-design.md](plans/04-design.md) (the design), [tests/DIVERGENCES.md](tests/DIVERGENCES.md) (every deliberate difference from diskcache), [upstream/README.md](upstream/README.md) (diskcache bugs we found, with repros). Published docs: <https://mkennedy.codes/docs/diskcache-oxide/>.

## Layout

- `crates/diskcache-oxide-core/`: the storage engine. No PyO3. Deals in keys, modes, bytes and value files. `#![deny(unsafe_code)]`, allowed only in `stack` (lock-free stack) and `sqlite_mutex` (our pthread mutexes for SQLite).
- `crates/diskcache-oxide-py/`: the `diskcache_oxide._core` extension (PyO3). `Cache` and `FanoutCache` are frozen pyclasses; `codec/` owns every Python-object-to-bytes conversion (msgpack, JSON, pickle fallback).
- `python/diskcache_oxide/`: the typed package. `__init__.py` re-exports `_core`; `null.py` (`NullCache`), `caches.py` (`Caches` registry), `protocol.py` (`CacheLike` and friends) are pure Python. `_core.pyi` is hand-written and holds the full docstrings; `tests/unit/test_stubs.py` keeps it honest against the compiled module. The `.so` files there are build output.
- `tests/conformance/`: black-box suite that runs against diskcache 5.6.3 (the oracle), the diskcache-community fork, or oxide, through `backends.py` adapters. `tests/unit/`: oxide-only tests.
- `bench/`: benchmarks (`baseline`, `gate`, `fanout`, `memory`, `wal_burst`, `fork_cost`); results in `bench/results/`, comparable only on the same machine.
- `soak/`: the 24-hour chaos soak ([soak/README.md](soak/README.md)). `scripts/`: everything CI would run, plus model checking, sanitizers, PGO and docs.
- `plans/`: one plan per phase or spike. `reviews/`: deep reviews. `upstream/repros/`: standalone diskcache bug repros.
- Docs sources live in `docs-source/`: `index.qmd`, `user_guide/*.qmd`, `recipes/*.qmd`, `assets/`, `skills/diskcache-oxide/SKILL.md` (the hand-written agent skill). `great-docs.yml` stays at the root, and `scripts/build_docs.py` points Great Docs at `docs-source/`. `site/` is the built site and is committed. `great-docs/` is the ignored build dir.

## Environments

One environment per Python, built and refreshed with `uv pip install`, **never `uv sync`**: it uninstalls whatever isn't in the groups it syncs (the docs tooling, ipykernel, anything installed by hand). `scripts/uv_env.sh`'s `dco_env <env> <python>` is how every script builds one: it creates the venv if needed, installs the dependencies at the versions `uv.lock` pins (`uv export --locked`), then the project, editable. `uv lock` is fine; it never touches a venv.

| Dir | Python | Use |
|---|---|---|
| `venv/` | 3.14 | default; `make` targets use it |
| `.venv-ft/` | 3.14t | free-threaded |
| `.venv-315/`, `.venv-315t/` | 3.15, 3.15t | |
| `.venv-mem/` | 3.14t | memory-instrumented SQLite build, created by `check.sh` |
| `.venv-soak/`, `.venv-soak-ft/` | 3.14, 3.14t | created by `soak/run.sh` |

```bash
bash -c 'source scripts/uv_env.sh && dco_env venv 3.14'        # a new or refreshed venv/: locked deps, then the project (editable)
bash -c 'source scripts/uv_env.sh && dco_env .venv-ft 3.14t'
uv pip install --python venv/bin/python -e .                     # rebuild the extension after a Rust change
uv pip install --python venv/bin/python --group docs             # the docs tooling (Great Docs, ipykernel)
```

`uv pip install -e .` rebuilds the extension when any `.rs`, `Cargo.toml`, `Cargo.lock` or `.cargo/config.toml` changes (`tool.uv.cache-keys`); add `--reinstall-package diskcache-oxide` to force it. The Rust toolchain is pinned in `rust-toolchain.toml` and tracks the latest stable; `rust-version` in `Cargo.toml` moves with it. No older MSRV, so use new std APIs freely. Quarto must be on `PATH` for docs; `cargo-deny` and the pinned nightly (for Miri) are separate installs.

## Commands

```bash
# Python tests. Plain pytest runs the conformance suite against diskcache (the oracle), not oxide.
venv/bin/pytest                                          # unit tests (oxide) + conformance against diskcache
venv/bin/pytest --backend=oxide tests/conformance        # conformance against oxide (or DCO_BACKEND=oxide)
venv/bin/pytest --backend=community tests/conformance    # the diskcache-community fork as a second witness
venv/bin/pytest -m slow                                  # heavy stress tests, excluded by default
.venv-ft/bin/pytest --backend=oxide tests/conformance    # same on 3.14t; run both before calling a step done

# Rust
cargo fmt --all --check
cargo clippy --all-targets -- -D warnings
cargo test
RUSTDOCFLAGS="-D warnings" cargo doc --workspace --no-deps
cargo deny check
scripts/model-check.sh [iterations]      # shuttle models of the fork gate and write lock (own target dir)

# Lint and types (ty, never mypy)
venv/bin/ruff format --check . && venv/bin/ruff check .
venv/bin/ty check --python venv

# Everything CI would run, on 3.14, 3.14t, 3.15 and 3.15t (add --slow for the slow tests)
scripts/check.sh

# Benchmarks (AC power, nothing else heavy running)
venv/bin/python -m bench.gate            # latency and throughput gate vs diskcache, 3.14
.venv-ft/bin/python -m bench.gate        # 3.14t

# Docs: builds with Quarto + Great Docs (>=0.17) and mirrors into site/ (committed). Needs the docs group in venv/:
# `uv pip install --python venv/bin/python --group docs` (Great Docs runs the docs' code cells in venv/).
make docs
make docs-serve

# Release artifacts and gates
scripts/build_pgo.sh [3.14 3.14t 3.15 3.15t]   # PGO wheels into dist/
scripts/build_linux.sh [--arch aarch64,x86_64] [python ...]   # the same in manylinux_2_28 Docker (x86_64 via Rosetta)
make wheels                                     # all four Pythons: macOS arm64, then Linux aarch64 and x86_64
make wheels-mac / make wheels-linux             # one platform
make wheels-local                               # macOS, 3.15 only, for a quick local wheel
scripts/sanitize.sh [--lanes A,B,C,D,E] [--quick]   # Miri, TSan, ASan lanes; results in sanitize-results/
scripts/release_tier.sh [base]                  # which release gate the changes since base need (see Release tiers)
soak/run.sh --hours 24                          # the soak; soak-results/latest/status.txt is the live view
```

Every heavy job on the Mac runs under `nice -n 20` so the machine stays usable, except benchmarks whose timings matter (nice skews them; ask first). Long soaks and sanitizer runs belong on the Mac mini, one at a time.

## Conventions

**Rust.** Clippy pedantic is on; `unwrap_used` and `dbg_macro` are denied in production code (tests may unwrap). rustfmt at 120 columns. Errors via `thiserror`. Every SQLite open goes through `sqlite::open*`, which installs our mutex methods first; a grep test bans direct opens and raw C API calls. Keep `panic = "unwind"` (PyO3 turns panics into `PanicException`). The bundled SQLite's compile flags live in `.cargo/config.toml` and the smoke test in `check.sh` asserts them.

**Python.** ruff at 120 columns, single quotes, `py314` target. Exceptions are created under the `diskcache_oxide` name in `crates/diskcache-oxide-py/src/lib.rs` so they pickle across `multiprocessing`; `__init__.py` re-exports them. The library has no CLI and no environment variables; warnings go to the `diskcache_oxide` logger.

**Behavior.** diskcache 5.6.3 is the oracle. Any deliberate difference gets a `D-NNN` entry in `tests/DIVERGENCES.md` and a `@pytest.mark.diverges(ref=...)`, `backend_only(...)` or `covers(...)` marker; the conformance conftest fails collection on an unknown ref. A divergence is a decision, never a way past a failing test. A diskcache bug we find goes in `upstream/README.md` with a repro in `upstream/repros/`. Settings persisted in the database use `None` sentinels in constructors so "not passed" differs from "passed the default".

**Benchmarks.** Headline mix is 99 reads : 1 write, with the 1,000 : 1 "Talk Python profile" beside it, at 1, 2, 4 and 8 threads, in a share-nothing loop. Write-heavy runs are extra stress cases, labelled as such. Take no performance conclusions from the OrbStack VM; `scripts/linux-check.sh` and `linux-soak.sh` are for Linux-specific behavior only, and real Linux numbers come from an x86-64 host.

**Docs.** Guide pages tell the story; reference tables, docstrings and the agent skill stay plain. Every behavior claim in the docs must match the code and the tests. After changing a `.qmd`, a docstring or the skill, run `make docs` and commit the regenerated `site/`. `ruff` excludes `site/`.

**Markdown.** Never hard-wrap prose. One paragraph per physical line; newlines only for real structure (blank lines, list items, headings, code blocks). Keep that when editing existing files.

## Workflow

- Each phase or spike gets its own plan in `plans/` before work starts. The `the-rust-architect` agent (in `.claude/agents/`) reviews every plan and every finished phase; fold its feedback in. Work in small steps, one at a time, and interview Michael when a decision is genuinely his.
- A step is done when `cargo clippy`/`cargo test` pass and the suite is green on both 3.14 (`venv/`) and 3.14t (`.venv-ft/`), against oxide. `scripts/check.sh` before a phase ends.
- Commit and push to `main` at the end of each phase (standing approval). Mid-phase commits, other branches, force-pushes, filing upstream issues and publishing to PyPI need Michael to ask. Commit messages are one descriptive paragraph on a single line saying what changed and why (see `git log`); no `Co-Authored-By` lines.
- Record user-visible changes in `CHANGELOG.md` under Unreleased. The version lives in `Cargo.toml` (`workspace.package.version`); `pyproject.toml` reads it dynamically.
- Release: pick the gate from the tiers below, then PGO wheels (`make wheels`) and PyPI. PyPI already holds a `0.0.1` placeholder and a yanked `0.1.0.dev1`; those filenames can never be reused.

### Release tiers

The gate depends on what a change touches, not how big it is: a one-line change to the write lock is riskier than 200 lines in a codec. `scripts/release_tier.sh [base]` lists the files changed since the last release (the newest `v*` tag, or a commit you pass) and prints the tier they need. Its tier is a floor: when in doubt, take the higher one, and Michael can always ask for the full gate.

| Tier | What changed | Gate |
|---|---|---|
| None | Docs, recipes, the skill, scripts, benchmarks, tests that don't change shipped code | No release. `make docs` for docs; run what a script change affects |
| Patch | Python-only code, the codecs (without changing the bytes they write), error messages, `durable`, `check`, `stats`, `queue`, the PyO3 layer, an isolated Rust fix outside the full-tier areas | `scripts/check.sh` (all four Pythons), `scripts/sanitize.sh --quick`, a 2-hour soak in the release shape (`soak/run.sh --hours 2 --procs 7 --threads 4 --rate 180000 --fanout-threads 16`). Release, then run the 24-hour soak on the released commit; if it finds a problem, yank and fix |
| Full | Locks, the fork gate, `sync`, the lock-free `stack`, `sqlite_mutex` and `sqlite`, `lockwait`, `poison`, `pool`, `store`, `open`, the WAL, value files (`spill`), `rows`, `ops`, `maintenance` (eviction, `cull`, `clear`), any `unsafe`, the core's integration tests, `.cargo/config.toml` (SQLite's compile flags), `rust-toolchain.toml`, or a new SQLite, rusqlite or PyO3 | Every sanitizer lane, then a 24-hour soak on the final commit, before the release |
| Format | Anything that changes what's on disk: `schema`, key encoding, the fanout root, the bytes a codec writes | A minor bump (the last number still goes up: 0.2.3 to 0.3.4), the full gate, and upgrade notes first in the changelog entry (see `docs-source/recipes/upgrading.qmd`) |

Every soak, either length, uses the release shape: `--procs 7 --threads 4 --rate 180000 --fanout-threads 16`. Without `--rate` the load runs unpaced and saturates the disk. The patch soak is 2 hours, not 30 minutes, because of the record: the only soak that has failed (2026-11-07, run without `--rate`, so `durable` calls overran their budget) first failed at 1.17 hours and again at 1.58. Problems that build up under load take more than half an hour to show. Every other soak through 2026-11-10 passed. A 2-hour run's leak check can flag workers whose RSS rises and falls in a slow wave, since it's tuned for 24 hours; that flag is a WARN, not a failure.

## Gotchas

- `pytest` with no `--backend` tests diskcache, not oxide. The unit tests under `tests/unit/` are always oxide.
- `timeout_method = "signal"` in pytest is deliberate: the thread method breaks the fork tests.
- The fd-heavy tests need `ulimit -n 10240`; a shell started over non-interactive ssh gets macOS's 256 limit.
- Tarballs built on macOS need `COPYFILE_DISABLE=1 tar ...`, or `._*` AppleDouble files fail the source-scanning tests.
- `scripts/model-check.sh`, `scripts/sanitize.sh` and `check.sh`'s memory build each use their own `target/` subdirectory so they never invalidate the normal build.
- A value the chosen serializer can't represent raises `TypeError` at `set`; that is by design (D-001), not a bug to route around with `pickle=True`.
- `size_limit` below 1 MiB raises `ValueError` (D-005). `bool(cache)` is always True (D-009).
