# Plan 21: Release 0.1.0

Written 2026-11-08, while the release soak runs on `34b3796` on the mini (started 15:03 PST, ends about 15:03 on 2026-11-09). The goal is a `0.1.0` on PyPI that a stranger with diskcache's use case can install and run, not only Talk Python.

## Rule: the soaked code doesn't change

Everything in this plan is docs, metadata, the version string, and build output. Before the tag, an allowlist check on the bump commit:

- `git diff --name-only 34b3796 <bump>` lists only `README.md`, `CHANGELOG.md`, `index.qmd`, `user_guide/`, `recipes/`, `skills/`, `site/`, `plans/`, `pyproject.toml`, `Cargo.toml` and `Cargo.lock`. Nothing under `crates/`, `python/`, `tests/`, `soak/`, `bench/`, `scripts/`, `docker/`, `.cargo/` or `rust-toolchain.toml`.
- Then the hunks: `Cargo.toml` changes the one version line; `Cargo.lock` changes only the two workspace crates' versions (regenerated with `cargo check`, never `cargo update`); `pyproject.toml` changes only classifiers, keywords and project URLs.

The version is compiled in: `crates/diskcache-oxide-py/src/lib.rs` sets `__version__` from `env!("CARGO_PKG_VERSION")`, so the shipped `.so` differs from the soaked one by that string. That's accepted; Step 4 asserts `__version__ == "0.1.0"`. If anything else turns up, the code changed and it needs a fresh soak. A Rust or Python change found necessary by this plan goes into 0.1.1 or restarts the soak, Michael's call.

## Evidence it fits the core use case

- The Talk Python suite (9,102 tests, the app's `main` at `1345599cc`) ran against a wheel of `34b3796` in a scratch venv: 9,095 passed, 7 skipped, 1 failed. The failure (`test_excluded_ads_still_serve_on_episodes`, an ad-scheduling data check) fails the same way on the app's own `0.1.0.dev2` venv, so the library isn't the cause.
- A first-time-user review of the README, docs, CHANGELOG and package metadata (2026-11-08) found no missing license, broken site paths or TODOs, but every entry point still describes an uninstallable pre-release. Step 1 fixes what it found.
- The README's first example raised `NameError` when pasted (undefined `episodes`, `refresh_feeds`, no `import os`). The second ran clean.

## Decisions (Michael, 2026-11-08)

1. **Version bump to 0.1.0 after the soak** is approved, given the Talk Python result above. The bump is metadata on the soaked code.
2. **Wheels:** CPython 3.14, 3.14t, 3.15 and 3.15t on macOS arm64 (built on the Studio), Linux x86_64 (built on talk-python-htz, under `/root/diskcache-run`, nothing written to sdc) and Linux aarch64 (the mini's native arm64 Linux container, after the soak ends), all PGO, plus an sdist. No Intel Mac, musllinux or Windows wheels in 0.1.0.
3. **Format policy during 0.x:** a release that changes the on-disk format bumps the minor version, says so at the top of its changelog entry, and needs existing caches deleted; opening an older format raises `FormatMismatchError` (a `Caches` registry reports a `NullCache`). No migration code.
4. **The GitHub repo goes public with the release**, Michael's step, beside the PyPI upload. Until then the PyPI links 404 by design.

## Step 1: Docs and metadata pass

From the review, each item checked against the code before it's changed:

- **Status text:** README, `index.qmd`, `user_guide/getting-started.qmd`, `user_guide/roadmap.qmd` and the CHANGELOG preamble say `0.1.0`, `pip install diskcache-oxide`, which gates passed (conformance against diskcache 5.6.3, the sanitizer pass of plan 14, the 24-hour soak), and that a source build needs the latest stable Rust. Drop "until 0.1.0", "not yet on PyPI", "install from GitHub".
- **The wheel matrix** from decision 2, named on the install sections of the README and `index.qmd`.
- **Classifiers and keywords** in `pyproject.toml`: Development Status 4 (Beta), Python 3.15, CPython, POSIX Linux, MacOS, Topic Database and Software Development Libraries, keywords.
- **README links that break on PyPI:** `LICENSE` and the contributor pointer become absolute GitHub URLs; the contributor line points at the repository rather than at `CLAUDE.md` by name.
- **What isn't there yet, up front:** `memoize`, tags, `Deque`/`Index`, `AsyncCache`, non-`str` keys, `disk=` and eviction policies, near the top of the README and `index.qmd`, and the Python 3.14+ requirement on the README's first screen.
- **Internal language:** "Phase N" in the user guide and recipes becomes "planned" with a roadmap link; Talk Python's internal names in `caches.qmd` are rewritten as a generic app; `plans/` references and lane names leave the CHANGELOG.
- **CHANGELOG:** a `## [0.1.0] - 2026-11-09` (or the publish date) entry that summarizes what ships, written for a first release. The pre-release history under Unreleased is folded into it, short, without fixes to formats or bugs no released version ever had.
- **Upgrading:** decision 3 written in `storage-and-wal.qmd` and `switching-from-diskcache.qmd`, and the old-cache cleanup line names diskcache's files precisely (`cache.db*` and its two-level hex folders), so nobody deletes oxide's one-level `aa/` folders sharing the directory.
- **Directory permissions:** one sentence where the 0700 directory mode is explained, on what a job running as another user or container UID sees, checked against the real error.
- **The README quickstart** becomes runnable as pasted, and is run.
- **Private hooks out of user docs:** `getting-started.qmd` stops pointing at `cache._durable_stats()` (it stays as a test hook in the stub).
- **Performance page:** numbers taken in the Linux VM are labelled as such or removed, per the project's own rule.
- `make docs`, `site/` regenerated, every claim changed checked against code and tests.

## Step 2: Version and changelog

`workspace.package.version = "0.1.0"`, `Cargo.lock` regenerated, the CHANGELOG entry dated. One commit with Step 1, pushed to `main` once the soak passes.

## Step 3: Wheels

Every build runs from a clean checkout of the bump commit: each build log records `git rev-parse HEAD` and an empty `git status --porcelain`. Output goes into an empty `dist/0.1.0/` (the existing `dist/` holds dev wheels, a soak tarball and stale files that must never be uploaded).

- **macOS arm64:** a clean worktree of the bump commit on the Studio, `scripts/build_pgo.sh 3.14 3.14t 3.15 3.15t` under `nice -n 20`.
- **Linux x86_64:** on talk-python-htz in `/root/diskcache-run`. The private repo arrives as a `COPYFILE_DISABLE=1` tarball of the bump commit (`git archive`), checked by hash. Before building: `docker info -f '{{.DockerRootDir}}'` is on the root disk, not sdc; the out dir is `chown 1000` (the container runs as uid 1000, `linux-check.sh` creates `--out` as root); `--cpus` and `--memory` are passed, since `nice` doesn't reach inside the container. A cold image build pulls the nightly and clang, several GB. No production container, mount or network is touched.
- **Linux aarch64:** the same in the mini's OrbStack container after the soak finishes.
- Linux builds pass `--compatibility manylinux_2_34`, so a symbol that needs a newer glibc fails the build instead of quietly getting a newer tag (plan 10 recorded `manylinux_2_34` from this image, within Ubuntu 22.04's glibc 2.35).
- **sdist:** `uv build --sdist`, `COPYFILE_DISABLE=1`. `tar tzf` shows no `.so` or `.profraw`, and includes `Cargo.lock`, `LICENSE`, both crates and `.cargo/config.toml`; if maturin leaves the config out, a source build silently loses the SQLite flags (STAT4 on, about 30% slower sets), so add `include = [{ path = ".cargo/config.toml", format = "sdist" }]` (that edit is to `pyproject.toml`, inside the allowlist).
- Each wheel: `ldd` (or `otool -L`) shows only libc and libgcc, and it holds exactly one `_core` `.so` matching its tag.

## Step 4: Verify the artifacts

- Each wheel installs into a clean venv on its platform, and the non-slow unit and conformance suites pass against it, with `diskcache_oxide.__file__` in site-packages (so the repo's `python/` can't shadow it), `__version__ == "0.1.0"`, `sqlite_compile_options()` matching the bundled flags, and `sys._is_gil_enabled()` False after import on the free-threaded builds.
- One x86_64 wheel installs and imports in an `ubuntu:22.04` container, proving the tag.
- The sdist builds and installs from scratch on the Studio and passes the same `sqlite_compile_options()` check.
- On htz: plan 084 Step 12 precondition 4 (Michael's test) with the x86_64 cp314 wheel.
- The README quickstart runs against the installed wheel.
- `twine check --strict` on every file (it also renders the README as METADATA). A TestPyPI upload of the exact set, which catches rejected classifiers and a half-finished upload. 13 files, all `0.1.0`; `SHA256SUMS` regenerated.

## Step 5: Publish (Michael)

- The soak's PASS is recorded in plan 14; the allowlist check holds; the architect has reviewed the finished release.
- Annotated tag `v0.1.0` on the bump commit (not `34b3796`), pushed.
- Michael makes the repo public and uploads `dist/0.1.0/` to PyPI from his account. The upload command is handed over as one line with absolute paths.
- A GitHub release for `v0.1.0` with the changelog entry and the wheels attached (plan 10's 2026-11-06 decision), once the repo is public.
- Talk Python's plan 084 Step 12 proceeds from PyPI.

## Progress

- 2026-11-08: plan written; Talk Python suite and first-time-user review done (above). The architect reviewed the plan; its seven required changes (the allowlist diff, the compiled-in version, a clean `dist/0.1.0/`, clean-checkout builds and the tag on the bump commit, the htz out-dir owner, the sdist check, the TestPyPI rehearsal) and its suggestions (the manylinux pin, the 22.04 install, the GIL and shadowing asserts, htz's Docker root and limits, the GitHub release) are folded in.
- 2026-11-08: Step 1 under way: README, `index.qmd`, getting started, roadmap, switching, storage, caches, performance, the recipes and `pyproject.toml` updated; the README quickstart now runs as pasted.
- 2026-11-08: Step 1 and Step 2 done. The sdist was missing `.cargo/config.toml` (confirmed by `tar tzf`); `[tool.maturin] include` adds it, and a from-scratch sdist install now reports the same 51 SQLite compile options as the wheel (hash-identical, STAT4 off). Version `0.1.0`; `Cargo.lock` changed only the two workspace versions; the allowlist holds against `34b3796`; unit suite on 3.14: 1,263 passed. Dependencies checked: all current except arc-swap 1.9.4 (a use-after-free with `Weak` pointers we never store), rusqlite 0.41.1 (SQLCipher and zero-argument function fixes we don't use), shuttle 0.9.10 (model checking only) and base64 0.23 (major); none is taken in 0.1.0, since each changes the soaked code; they are the first change for 0.1.1. Michael approved committing and pushing the release commit before the soak ends.
