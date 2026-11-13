# Plan 27: Docs that hold up in production, and an example app

Written 2026-11-13, from a full read-only review of the docs against the code and tests. Its section 1 (outright errors) was fixed before this plan, with fresh performance numbers (`2e347aa`). This plan covers the rest: weak and contradictory guidance, the places readers get stuck, missing topics, and a runnable example app the docs point at and quote. Reviewed by the architect on 2026-11-13; its findings (H1-H5, M1-M11, L1-L6, gaps) are folded in below.

No library code changes. Docs, the example app, its tests, and their wiring into `scripts/check.sh`. Release tier: none.

## Decisions (Michael, 2026-11-13)

1. **Recommended production setup:** a `Caches` registry with `durable=True`, plain caches, no sharding (memory: one connection pool per cache, not one per shard). `FanoutCache` stays documented as the answer to write contention, not the default.
2. **Docker:** a named volume (recommended) or a host directory mapped in, and the Compose story in the guides and recipes.
3. **Missing topics go in:** troubleshooting by symptom, deployment (Docker, Compose, Kubernetes), logging setup.
4. **An example app** in this repo, referred to by the docs, beautiful without looking machine-made. **FastAPI.** The look is chosen from mockups: three directions plus one that matches the docs, all pages, in one artifact.
5. **Order:** run this plan while the 0.3.5 soak runs on the mini.

## Rules every page and the app follow (from the review)

- **State is never a registry cache (H2).** Sessions, job claims, locks and work queues are opened with `Cache(...)` directly, non-durable, `eviction_policy='none'`, a timeout of a few seconds. A registry hands out a `NullCache` on any failed open and on `disable()`, whatever `durable` says, and on a `NullCache` `add()` is True (every worker wins the claim), `push()` stores nothing, `set()` returns True (a login that doesn't stick) and `Lock` raises `TypeError`. `sessions.qmd:80-81` and the index sample change to match. Note that `register()`'s directory-collision check can't see directly opened caches.
- **One directory layout everywhere (H3, M9).** A directory is a cache or a parent of caches, never both: `check(fix=True)` treats a two-hex-character subdirectory as a value folder, so a cache named `ab`, `db` or `fe` nested inside another cache's directory can lose its database. The layout: one mount, `/var/lib/my-app/`, with `cache/` (the registry's base: durable caches) and `state/` (`sessions/`, `coordination/`, `jobs/`). `/var/lib`, not `/var/cache`, because state isn't cache and `/var/cache` may be wiped (`systemctl clean --what=cache`). The docs use this layout in every example (rewriting `/var/cache/my-app-locks`, `-coordination`, `-jobs`), and `coordination.qmd:11`'s rationale becomes the nesting rule.
- **The cache must be on a local filesystem (H1).** diskcache-oxide refuses only when SQLite itself declines WAL (some FUSE mounts). It can't detect NFS, SMB, EFS or Azure Files, where WAL is usually accepted and then corrupts. Never a ReadWriteMany volume. Fix the overclaims in `web-workers.qmd:11`, `errors.qmd:163` and `concurrency-and-forks.qmd:32`.
- **Timeouts stated (M5).** The durable registry gets `timeout=1.0`; state caches a few seconds. On a `Timeout` from state, the web app returns a 503 page and the worker retries.
- **No writes on the event loop, no waits on the request path (M1, M2).** `memoize` is for sync functions, called from `def` routes or `run_in_threadpool`, through the `memoized(name)` helper from `caches.qmd:98` (a decorator bound at import would hold a `NullCache` forever). `evict()` runs off the loop and raises `Timeout` even when durable; an `expire` is the backstop. A `Lock` waits forever, so it lives in the worker, never in a request; requests use `add()` claims, which skip.

## Part 1: One production story

- `web-workers.qmd` opens with a **Recommended setup**: the durable registry (`timeout=1.0`, plain caches) under `/var/lib/my-app/cache`, state caches opened directly under `state/`, `close_all()` and `close()` in the shutdown hook. The per-framework sections use it. Fix `web-workers.qmd:86`: `uvicorn --workers` uses spawn on every platform, so it isn't a pre-forking server; note that `multiprocessing` defaults to forkserver on Linux since 3.14 (`web-workers.qmd:74`).
- One "what isn't durable, and why" section, linked from the others.
- `caches.qmd:64`: a short rule, a bullet list, an example.
- The skill and README follow, in fewer words.

## Part 2: Getting started that gets people started

- First examples run on a laptop: a local directory. One paragraph on production (`/var/lib/<app>/cache`, created and owned by the app's user; systemd's `StateDirectory=`/`CacheDirectory=` for non-container deploys) and on Windows (below).
- The `on_decode_error` × `durable` table and the `pop` internals move to `errors.qmd` ("Reading damaged values"); three sentences and a link stay.
- The Install section gets one paragraph on upgrades and format changes.
- Testing recipe: a `NullCache` never encodes values, so a value a real cache refuses passes; one real-cache test per stored type.

## Part 3: Where readers get stuck

**Switching from diskcache.** A table mapping diskcache's settings keywords (`statistics=`, `tag_index=`, `sqlite_mmap_size=`, `disk_min_file_size=`, `disk_pickle_protocol=` and the rest of `DEFAULT_SETTINGS`) to oxide's equivalent or "dropped, because". State that isn't cache: link the upgrading recipe's section, plus a short copy loop from a diskcache directory with its caveats. Django: keep Django's cache on diskcache until the backend ships.

**A new recipe, "Docker and Compose" (`recipes/docker.qmd`)**, linked from web-workers and Getting started:
- `compose.yaml` with one named volume mounted at `/var/lib/my-app` for the web app and the worker; the bind-mount alternative explained, not used in the example.
- The Dockerfile: `python:3.14-slim` (no musl wheels), a fixed non-root UID that never changes across image versions (the `0700` directory locks out a previous owner, D-015), and `mkdir` + `chown` of the mount point before `USER`, because a named volume copies the image's ownership only on the first mount of an empty volume. Bind mounts and Kubernetes PVs don't copy: a missing host path is created by dockerd as root (`PermissionError`); use `fsGroup` or an initContainer on Kubernetes.
- Sharing a cache between the host and a container through Docker Desktop's file sharing (virtiofs on macOS): verify what actually happens before saying anything; say only what was seen.
- Kubernetes, precisely: an `emptyDir` is per pod; `hostPath` or a local PV is per node, shared by that node's pods (same UID); a StatefulSet's ReadWriteOnce PVC is per pod and persists; never ReadWriteMany.
- Disk sizing: the sum of `size_limit`s, plus about one WAL per cache (a target, not a bound: D-024), plus the unbounded `'none'` state caches.
- Read-only root filesystems: `VACUUM` needs `SQLITE_TMPDIR`.
- Backups: caches need none; sessions and queues are state, and a live WAL database is never `tar`ed.
- Deploys: rolling and blue/green deploys sharing the volume can cross a format change (link the upgrading recipe). The cleanup step is `docker compose run --rm --no-deps app python deploy_cleanup.py`, run after `up -d` has replaced every old container, never at worker startup. It cleans the registry's caches and the directly opened state caches, and drains the old queue first (`peek`).

**Logging (M4).** A `logging.config.dictConfig` snippet that names `diskcache_oxide`, sets `disable_existing_loggers: False` (the default `True` silences the library's logger, which exists from import, and with it every durable outage report), and a formatter that renders the redirect warning's `extra` fields. gunicorn's `logconfig_dict` in the example.

**Rate limiting behind a proxy (M3).** Proxy headers through uvicorn's `forwarded_allow_ips` (ProxyHeadersMiddleware), not a hand-parsed `X-Forwarded-For`; in Compose the proxy is another container's IP, so the `127.0.0.1` default won't match. The limiter writes on every request, so it's a sync dependency; it fails open on a durable handle or a `NullCache`, and says so; it uses its own durable registry cache with least-recently-stored eviction, never `'none'`.

**Windows (M8).** Only what's verified. In: `%LOCALAPPDATA%` for a per-user app, and `platformdirs.user_cache_dir` (`%LOCALAPPDATA%\<author>\<app>\Cache`); a warning against `C:\ProgramData` (by default Users can create there, so anyone could poison the cache) with an `icacls` line; the antivirus exclusion as a speed option only (plan 25's stress run passed with Defender on), with its security trade-off. Out unless a Windows session verifies them first: IIS and app-pool recycles, waitress and hypercorn, Celery, `%LOCALAPPDATA%` under a service identity, and what a Windows-on-ARM source build needs. A Windows verification step (step 2b) can add them back.

## Part 4: Missing topics

- **Troubleshooting by symptom** (`user_guide/troubleshooting.qmd`): every lookup misses (a registry `NullCache`; a redirect to `diskcache-oxide-format-N`; a type not registered in this process, which a durable cache silently misses because `on_decode_error` defaults to `'miss'`; a patched clock); the disk keeps growing (the WAL, `'none'` caches, a `FanoutCache`'s per-shard logs, an old format not yet removed; sizing as above); writes slow under load (`FanoutCache`); `PermissionError` (another user, a script run as root first via sudo, root cron or `docker exec -u root`, Windows open handles); which format a cache is in (`cache.directory`, or `PRAGMA user_version`). Each links its detail page.
- **Monitoring (L1, L2):** `stats()` is per process (D-049), so a dashboard under 4 workers shows one worker's numbers: say so, or use the shared-counter pattern. `caches.stats()` count and volume are true across processes. A Prometheus collector that reads them at scrape time (no multiprocess mode needed for those). `/health/cache` is a readiness check, not liveness. Admin endpoints need auth or a local-only bind.
- **Frameworks:** the example app is the FastAPI story; Litestar drops off the index's list unless it gets an example.

## Part 5: The example app (`examples/webapp/`)

**The app.** A podcast episode browser with a deliberately slow fake "database" (visible delay, so caching is obvious): an episode list, an episode page, search, sign-in, an editor, a jobs page and a cache dashboard. FastAPI, server-rendered Jinja templates, one stylesheet, no front-end build.

**What it shows**, each pointing at the doc page that explains it:
- the durable registry under `/var/lib/my-app/cache` (`timeout=1.0`, plain caches): `pages` (shared fragments only, never per-user ones: M6), `api` (an upstream call through the HTTP recipe, with a fake upstream in tests), `rate_limits` (least-recently-stored);
- state caches opened directly under `state/`: `sessions`, `coordination` (claims and the worker's `Lock`), `jobs` (the queue), each non-durable, `'none'`, a few seconds' timeout, a 503 page on `Timeout`;
- `cache_types.py` with the `register_type` calls, imported by the web app and the worker, and a test that both register the same types (`registered_types()` after `python -I -c 'import worker'` and after `import app`: L4);
- `memoize` on the slow loader via `memoized(name)`, called off the loop;
- tags and `evict(tag)` behind the editor's save, off the loop, with `expire` as the backstop;
- the rate limiter (sync dependency, proxy-aware via `forwarded_allow_ips`, fails open and says so);
- an hourly `add()` claim on the request path; `worker.py` pulling jobs, with the crash-safe "running" record and a `Lock` around regenerating one file (a sitemap);
- `/health/cache` (readiness) and `/admin/caches` (local-only), the dashboard labelled per-process where it is;
- `close_all()` and the state caches' `close()` in the lifespan; `stats(enable=True)` in the lifespan; `logconfig_dict` routing `diskcache_oxide`;
- session cookies `HttpOnly`, `SameSite=Lax`, CSRF on the editor (L6);
- `gunicorn.conf.py` with `uvicorn_worker.UvicornWorker` (the `uvicorn-worker` package; `uvicorn.workers` is deprecated), a Dockerfile and `compose.yaml` as in Part 3, and `deploy_cleanup.py`.

**How it looks.** Michael picked direction A, "Ledger", from the mockups (https://claude.ai/artifact/5kihGH6LnGZAfqeRkVwLx3): an instrument panel in IBM Plex Sans and Plex Mono, white ground, near-black ink `#16181d`, hairline rules `#e4e5e7`, tabular numbers, one orange accent `#c2410c` for misses and links on hover, blue `#1d4ed8` for hits, square controls; a request strip under the header on every page lists that request's cache reads (cache, key, hit or miss, time), and pages put "how this was served" beside the content. Also: a real type scale, restrained colour, light and dark, a layout that shows the cache working (hit or miss and the time per request, the registry's numbers), and none of the tells of generated UIs.

**Installing diskcache-oxide in the image (H5).** The example pins `diskcache-oxide==<workspace version>`; a build arg installs a local wheel from `dist/` before that version is on PyPI. A Docker smoke test (build, `compose up`, poll `/health/cache`) goes in a new `scripts/example-docker.sh`, run at release; `check.sh` doesn't build images.

**How it stays tested (H4, M11).**
- A non-default `example` dependency group in the root `pyproject.toml` (FastAPI, Jinja2, gunicorn, `uvicorn-worker`, httpx), locked in `uv.lock`. `dco_env` is unchanged.
- In `check.sh`, on 3.14 and 3.14t only: install the group with `uv export --locked --only-group example --no-emit-project --no-hashes --format requirements-txt | uv pip install --python "$env/bin/python" -r -`, then `pytest -c examples/webapp/pyproject.toml examples/webapp/tests` (the app's own pytest config), and `ty check --python "$env" examples/webapp`. `examples/` is excluded from the root ty run (`[tool.ty.src] exclude`); ruff already covers it.
- Tests: TestClient tests for every page and endpoint against `tmp_path`; the type-registration test; a logging test that a `diskcache_oxide` WARNING reaches the configured handler; a multi-process smoke test (Unix only) that starts gunicorn with 4 workers on a Unix socket in `tmp_path`, polls for readiness, runs in its own session and kills the process group in `finally`, and asserts invariants, not distribution: exactly one claim winner across N concurrent requests, exactly `limit` requests allowed, each job processed once by a separate `worker.py`. The claim period and rate window are injectable, so a window boundary can't split counts.
- The app stays out of the wheel (`python-source = "python"`) and, checked rather than assumed, out of the sdist (`maturin sdist` plus a `tar tzf` check in `check.sh`).
- Docs quote it by named regions, not line numbers (L3): `# region: name` / `# endregion` markers in the app, extracted by `build_docs.py` (or the include-code-files extension). Spiked at the start of step 3.

**Size.** About 700 lines of Python with proper `Timeout` handling (L5), plus templates and one stylesheet.

## Steps

0. **Design:** mockups of all pages in three directions plus one matching the docs, in one artifact; Michael picks.
1. Part 1 and Part 2, with the rules above applied to every page; `make docs`.
2. Part 3 and Part 4. 2b (optional): a Windows verification session for the deferred Windows claims.
3. Part 5: spike the region quoting; the app, its tests, the `example` group and `check.sh` wiring, the sdist check, `scripts/example-docker.sh`; then the doc pages that quote it.
4. A final read of every changed page along the five journeys (new to caching, switching, web deploy, upgrading, Windows); `make docs`; `scripts/check.sh`; the architect's review.

## Done when

`make docs` builds with no errors and `site/` is committed; `scripts/check.sh` passes with the example's tests on 3.14 and 3.14t and the sdist check; `scripts/example-docker.sh` passes once; ruff and ty pass; every changed claim matches the code and tests; the architect has reviewed the finished change.

## Library follow-ups found here (not this plan)

- `check(fix=True)` can delete the database of a cache nested in a two-hex-named subdirectory of another cache (`check.rs:193, 241`): it should skip a hex directory that holds a `diskcache-oxide.sqlite`. Until then the docs' nesting rule routes around it.
- NFS/SMB detection (statfs `f_type`) if refusing them, rather than documenting, is wanted.

## Log

- 2026-11-13: plan written from the docs review; decisions 1-5 from Michael (FastAPI; the look from mockups).
- 2026-11-13: Michael picked mockup direction A, Ledger.
- 2026-11-13: architect review folded in: H1 (no NFS detection; fix three overclaims), H2 (state never through the registry), H3 (one `/var/lib/my-app` layout with `cache/` and `state/`; the nesting rule), H4 (`example` group, `check.sh` wiring, ty exclusion), H5 (pinned version and local-wheel build arg; a Docker smoke script), M1-M11 and L1-L6, and the production gaps (sizing, PermissionError causes, backups, read-only roots).
