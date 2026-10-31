# Phase 3 handoff: moving Talk Python to diskcache-oxide

Where things stand on 2026-10-30, and everything needed to pick up the port without the conversation that produced it. Read sections 1-3 to get oriented, and use the rest as reference while working.

Paths: `LIB` = `~/github/mk/diskcache-oxide`. `APP` = `~/github/talk-python/talk-python-to-me/src/talk_python_to_me_com`. `PKG` = `APP/talk_python_to_me_com`. Line numbers were read on 2026-10-30 and will drift. The Talk Python branch is `diskcache-oxide-cache`, and **nothing has been changed on it yet**.

## 1. Status

| Piece | State |
|---|---|
| Core `Cache`, thread-, process- and fork-safe | Done (Phases 2, 2.5). Gates are GO on macOS and Linux, 3.14 and 3.14t |
| Plan 08: `on_decode_error`, `ForeignValueError`, per-call `timeout=`, `Caches` | Done, committed, architect-reviewed |
| Plan 09 step 1: `pop` keeps values it can't read | **Designed and reviewed, not built** |
| Plan 09 step 2: `Cache(durable=True)` | **Designed and reviewed, not built** |
| Plan 09 step 3: `pop` in the soak kit, then a fresh 24-hour run | **Not started** |
| 24-hour soak on the mini | Running on commit `0f1b9b8`, clean at 9.3 h. It's to be stopped and replaced (section 8) |
| PGO wheels | Built into `LIB/dist/`, macOS arm64 only (section 4) |
| Linux wheel, PyPI release | **Neither exists.** Version is still `0.0.0`. Blocks production, not local work |
| Port plan | **Not written.** Section 6 has the raw material; it goes in `plans/` and gets the architect's review |

Plan documents: `plans/00-roadmap.md` (Phase 3), `plans/06` (the concurrency work), `plans/08` (what's built, with the reasoning), `plans/09` (what's next, including the review's required changes).

## 2. Decisions Michael has made

- **`Cache(durable=True)` is the design:** "durable means DURABLE as much as we can deliver". On a single-key call, any operational failure (timeout, disk full, I/O, SQLite error, a forked child that can't write) is reported and answered as a miss or False. `on_decode_error` defaults to `'miss'`. Admin calls (`clear`, `expire`, `cull`, `checkpoint`, `len`, `volume`, iteration), bugs, and calls inside a hold on that store still raise.
- **`delete` returns False under `durable=True`.** The risk is accepted: a lost invalidation serves stale data, and the log line is the signal.
- **`ForeignValueError`** keeps its name.
- **`pop` gets fixed** so it doesn't destroy a value it can't read.
- **A failed open is retried every 10 s** (`retry_open_after=10.0`).
- **Self-healing reads are a constructor option, and the registry is called `Caches`.** Purposes, the settings file, Sentry and logbook stay in the app.
- **The soak restarts once plan 09 is built:** a fresh 24 hours on that commit, replacing the 6-8 hour re-soak. The mini is Michael's to give; it's currently dedicated to this.
- **No pickle in Talk Python: msgpack only, and the app adjusts.** The two dataclass values become plain dicts (section 5, step 3). No cache opens with `pickle=True`.
- **Use `ty`, never mypy,** in the library too.

## 3. What to do next, in order

1. **Michael's perf test** (section 5), against the wheels already in `dist/`. It needs a small spike on the branch, because the app can't use the library unmodified.
2. **Build plan 09** in the library, with the required changes in its review section. Steps 1 and 2 first, each reviewed by `the-rust-architect` and committed.
3. **Add `pop` and durable lanes to the soak kit,** stop the current soak, start 24 hours on that commit. Rule: one soak at a time on the mini. Use `zsh -lc` over ssh, and see the memory note `soak-on-mini`.
4. **Write the port plan** from section 6, settling section 7's decisions with Michael. The architect reviews it.
5. **Port on the branch.** Run `./venv/bin/ty check`, `./venv/bin/ruff check --fix talk_python_to_me_com/`, `./venv/bin/ruff format talk_python_to_me_com/` and the tests, from `APP`. About 8,500 tests, serial, and `tests/test_type_check.py` fails on any `ty` diagnostic.
6. **Before production:** the soak PASS on the final library commit, a Linux wheel or PyPI release (section 9), and the cold-cutover steps (section 7, items 1 and 2).

Library changes during the port: Python-only changes such as `Caches` shouldn't need another soak. A change to `get_with`, `pop` or the locking does.

## 4. The PGO wheels

`scripts/build_pgo.sh [python ...]` (default 3.14, 3.14t, 3.15, 3.15t) builds instrumented, trains on `bench.baseline --quick` plus `bench/pgo_train.py`, merges the profiles, and rebuilds. Each interpreter gets its own profile, since GIL and free-threaded builds compile different code. In `LIB/dist/`:

```
diskcache_oxide-0.0.0-cp314-cp314-macosx_11_0_arm64.whl
diskcache_oxide-0.0.0-cp314-cp314t-macosx_11_0_arm64.whl
diskcache_oxide-0.0.0-cp315-cp315-macosx_11_0_arm64.whl     <- Talk Python's dev venv (3.15)
diskcache_oxide-0.0.0-cp315-cp315t-macosx_11_0_arm64.whl
```

- **Contents:** library commit `9241763`, so plan 08 and not plan 09. `dist/` is git-ignored. Rebuild after plan 09.
- **Verified:** each wheel is free of instrumentation, links the bundled SQLite with the shipped flags, and passes the full suite and conformance tests from a fresh venv (811 and 810 passed, 316 conformance).
- **Rust only.** The bundled SQLite is C, built by the system compiler (Apple clang, LLVM 21), whose profile format doesn't match rustc's (LLVM 23), so it isn't profile-guided. Much of a cached `get` is SQLite and its `fcntl`, which limits what PGO can do.
- **Measured benefit:**

Median p50 of 5 interleaved rounds on the laptop, PGO against a plain release build (lower is better, except throughput):

| Workload | 3.14 plain | 3.14 PGO | Ratio | 3.15 plain | 3.15 PGO | Ratio |
|---|---|---|---|---|---|---|
| `get_hit_small` (µs) | 0.933 | 0.877 | 0.94 | 0.930 | 0.874 | 0.94 |
| `get_hit_small_dict` (µs) | 1.353 | 1.211 | 0.90 | 1.341 | 1.209 | 0.90 |
| `get_miss` (µs) | 0.800 | 0.773 | 0.97 | 0.796 | 0.769 | 0.97 |
| `get_random_wide` (µs) | 2.884 | 2.783 | 0.97 | 2.872 | 2.755 | 0.96 |
| `get_page_entry_450k` (µs) | 27.7 | 27.2 | 0.98 | 27.2 | 27.5 | 1.01 |
| `set_small` (µs) | 20.3 | 19.9 | 0.98 | 20.0 | 21.1 | 1.06 |
| `set_page_entry_450k` (µs) | 248 | 242 | 0.98 | 231 | 243 | 1.05 |
| `add_incr_counter` (µs) | 27.9 | 28.7 | 1.03 | 27.5 | 28.7 | 1.04 |
| `threads_99to1`, 1 thread (ops/s) | 616,846 | 638,512 | 1.04 | 625,179 | 652,278 | 1.04 |

So small reads gain 3-10%, single-thread 99:1 throughput gains about 4%, and writes and large values are within noise (2-6% either way, in both directions, in only 5 rounds). The free-threaded wheels weren't measured. A tiny gain against a cost measured in milliseconds won't show up in a page-level test.

## 5. Perf test recipe (a spike, not the port)

Not run yet. It's the smallest change that gets the running app onto oxide, read from the code; expect to adjust it.

```bash
cd $APP
uv pip install ~/github/mk/diskcache-oxide/dist/diskcache_oxide-0.0.0-cp315-cp315-macosx_11_0_arm64.whl
```

Then on the branch:

1. **`infrastructure/durable_cache.py`:** `import diskcache_oxide as diskcache`, and fix the forwarding methods that pass arguments the library doesn't take:
   - `touch` (:279), `incr` (:287), `add` (:295): drop `retry`.
   - `pop` (:298): `self._cache.pop(key, default)`.
   - `expire` (:305): `self._cache.expire()`.
   - `push` (:317): no such method and no production caller, so delete it or leave it uncalled.
   - `__exit__` (:324): pass the three exception arguments through.
2. **`infrastructure/cache_service.py`:** `import diskcache_oxide as diskcache`. No `pickle=True`.
3. **`viewmodels/home/homeviewmodel.py`:** store plain dicts. Without this the homepage raises `TypeError` on `set`. The app's `nosql.db_helpers` already does the work; tested with real `Episode`, `Guest` and `Review` objects through the library's msgpack, and they come back equal, nested dataclasses included. Only the runtime-only `_` fields (`_loaded_guests`, `_has_transcript`) are dropped, and the home viewmodels don't use them.

   ```python
   # _save_snapshot (:195-205)
   def doc(x): return to_doc(x, include_id=True) if x is not None else None
   home_cache.set(SNAPSHOT_KEY, {
       'latest_episode': doc(self.latest_episode),
       'latest_guests': [doc(g) for g in self.latest_guests],
       'reviews': [doc(r) for r in self.reviews],
       'upcoming_episodes': [doc(e) for e in self.upcoming_episodes],
       'upcoming_guest_lookup': {k: doc(g) for k, g in lookup.items()},
   }, expire=SNAPSHOT_TTL_SECONDS)

   # _restore_snapshot
   self.latest_episode = from_doc(Episode, snapshot.get('latest_episode'))
   self.latest_guests = from_doc_list(Guest, snapshot.get('latest_guests', []))
   self.reviews = from_doc_list(Review, snapshot.get('reviews', []))
   self.upcoming_episodes = from_doc_list(Episode, snapshot.get('upcoming_episodes', []))
   return {k: from_doc(Guest, g) for k, g in snapshot.get('upcoming_guest_lookup', {}).items()}
   ```

   And `list[FeaturedCourse]` (:316): `courses_cache.set(cache_key, [dataclasses.asdict(c) for c in courses], expire=...)`, with `[FeaturedCourse(**d) for d in cached]` where it's read back. The snippets above haven't been run in the app; the round trip of the models has.
4. **Expect a cold cache.** Oxide uses `diskcache-oxide.sqlite` and ignores the old `cache.db`, so the first requests regenerate everything, and the HTTP hash cache downloads remote assets to hash them. Warm up before measuring, and compare warm against warm.
5. **Rate-limit settings reset locally too** (section 7, item 1), since they live in a cache.

Comparison tips: production runs Granian with `--workers 1 --runtime-threads 1 --runtime-mode st`, and the existing load test is `locust -f load_testing/locustfile.py`. `bin/perf/diskcache_perf.py` writes to the real General directory with a 60 s timeout, so don't treat it as a clean benchmark. In production the difference should show most on page caches, which are reads, and very little where an uncached render dominates the request.

## 6. Port reference

**The good news:** app call sites are nearly drop-in. Keys are all `str`, and no production code uses `retry=`, `tag=`, `read=`, `transact`, `memoize`, `push`, `pull`, `Index`, `Deque` or `FanoutCache`. Almost all API incompatibility is inside `DurableCache`, which goes away.

**Corrections to what plan 08 and I said earlier:** there are 30 non-test `create_cache` sites (28 in the app, 2 in `bin/`), not 52. And `size_limit` is passed at exactly one of them, `services/show_page_cache_service.py:38-40` (`10 * 1024**3`), not at many.

**Mapping:**

| Today | After |
|---|---|
| `DurableCache(name, diskcache.Cache(...))` | `Cache(..., durable=True)` through `Caches` |
| `CacheProtocol` (20 modules) | `CacheLike`; keep `ty` at 0 |
| app `NullCache(name)` on open failure | library `NullCache`, from `Caches`, with `on_open_error` wired to Sentry |
| `PerfTestNullCache(name, real)` | `NullCache(invalidate=real)` |
| `create_cache(name, size_limit, purpose)` | `caches[name]` plus the app's own purpose wrapper (below) |
| `timeout=REQUEST_CACHE_TIMEOUT` on every open | `Caches(base_dir, timeout=1.0, ...)` |
| maintenance handles with a 10 s timeout | `caches.clear(name, timeout=10)`, `caches.expire_all(timeout=10)` |
| `get_all_caches()` | `caches.stats()` plus descriptions held in the app; skip User Agents, which the admin page deliberately omits |
| `close_all_caches()` (`talk_python_app.py:264`) | `caches.close_all()`; returns `None`, not a count |
| `expire_all_caches()` | `caches.expire_all()`; returns `int \| None` per cache |
| `typings/diskcache/` (7 files, 3,108 lines) | delete; keep `typings/` on the checker paths for the other stubs |

**Physical caches and their directories.** Registry names are directory names, so the display names (`'General Cache'`) need a map in the app:

| Display name | Directory | Sites |
|---|---|---|
| General | `general` | 21 |
| Show Page | `show_pages` | 1 (10 GiB) |
| Episode Data | `episode_data` | 3 |
| HTTP Hash | `hashes` | 1 |
| MP3 Validation | `mp3s` | 1 |
| User Agents | `user_agents` | 1 |
| Rate Limit | `rate_limits` | 1, lazy |
| Rate Limit Config | `rate_limit_config` | 1, lazy |

**Purposes** (18, on 8 caches, off per purpose) don't fit `caches.disable`, which is per physical cache. Use the guide's pattern, building the wrapper on each call and not memoizing it:

```python
def cache_for(purpose: CachePurpose) -> CacheLike:
    real = caches[PURPOSE_CACHES[purpose]]
    return NullCache(invalidate=real) if purpose in disabled_purposes else real
```

**Import-time opens:** 19 modules hold a module-level handle (home, feed, app_user_agents, markdown_utils, cache_buster, link_builder, user_agents, transcripts_admin, mp3, episode_html, pro_subscriber, show_page, link_validation, transcript, rss, guide_html). A module-level handle that was a failed-open `NullCache` never picks up the 10 s retry. Likewise `rate_limit_records.py:106` and `rate_limit_config.py:153`, which memoize. Moving these to lookups at call time gets the recovery. The import-time `expire_all_caches()` sweep (`cache_service.py:206,470-473`) goes away.

**Behavior changes, app `NullCache` to library:**

| Method | App | Library |
|---|---|---|
| `add` | False | True |
| `delete` | True | False (forwarded with `invalidate=`) |
| `touch` | True | False |
| `incr` | 0 | `default + delta`; `KeyError` if `default=None` |
| `.name` | exists | gone |

What that does to callers:
- **Scheduled job claim** (`services/scheduled_jobs_service.py:256-259`): the roadmap's worry doesn't apply, because the `isinstance(cache, NullCache)` guard at :257 raises before `add`. Keep the guard, using the library class or `not cache.enabled`. Under `durable=True`, a real cache's `add` returns False on failure, so claims fail closed, and a swallowed `delete` leaves a lease until it expires. Claims should use a **non-durable** handle.
- **Sponsor throttle** (`services/sponsor_inquiry_service.py:174-179`): **the real behavior change.** On a failed open it goes from False (no confirmation email) to True (unthrottled email). That matches the intent at :171 but loses flood protection. Decide deliberately.
- **Rate limiting** (`services/rate_limit_records.py:195-202,221-235`): fail-open is preserved. `add` is True, `incr(default=None)` raises `KeyError` twice, and `bump` returns 0. `tests/test_rate_limit_service.py:88` (`incr == 0`) must change.
- **`delete` return values** report counts or flags in `rss_service.py:249`, `episode_html_service.py:262`, `pro_subscriber_service.py:610`, `guide_html_service.py:99`, `show_page_cache_service.py:381`. Plain `NullCache` gives False now. Pinned at `tests/test_cache_service.py:129-130,381-382`.
- **Home snapshot gate** (`homeviewmodel.py:192`): passes under the `home` flag, then `set` no-ops. Harmless.

**Direct `diskcache` use to change:** `bin/verify_episode_external_urls.py:8,564` (and :570 hard-codes `cache.db`), `bin/explore_diskcache.py`, `bin/perf/diskcache_perf.py`, `viewmodels/admin/server_info_viewmodel.py:30` (`KEY_PACKAGES`), `bin/run_granian_local.py:154-158` (ignore patterns cover `.db*` but not `.sqlite*`; `--reload-ignore-dirs db` saves it for now), and `requirements.piptools`, `requirements-development.piptools`.

**Tests to change:**
- Real-cache: `test_cache_service.py`, `test_scheduled_jobs_service.py`, `test_rate_limit_service.py`, `test_markdown_to_safe_html.py`, `test_audio_summaries_feed.py`.
- Clock patching: only `test_scheduled_jobs_service.py:76-79` (`_later`, used at 151-267). The fixture at :44 and handle at :135 need `clock=lambda: time.time()`.
- Doubles carrying `retry` or `push`: `rate_limit_doubles.py:85,93,102`, `test_rate_limit_service.py:532,657,873-877`, `test_home_viewmodel_resilience.py:45`, `test_cache_service.py:543-552`.
- Library `NullCache` uses `__slots__`, so shortcuts that skip `__init__` hit `AttributeError`: `test_cache_service.py:270`, `test_youtube_thumbnails.py:1116`, and subclasses at `rate_limit_doubles.py:52`, `test_cache_service.py:543`, `test_rate_limit_service.py:401,432,528,567,653,863,1173`.
- AST and name guards: `test_app_module_shape.py:38,50` pins `init_diskcache` and `init_diskcache_set`; `test_cache_service.py:515-541` pins one `create_cache` with no purpose.
- Fake-cache tests accept anything, so **none would catch a value msgpack can't store**. Add a test that sends real homepage values through a real `Cache`. `tests/test_home_viewmodel_resilience.py:130-131` (`is EPISODE`) has to change, since the snapshot now holds dicts. The comments at `homeviewmodel.py` and `:175` of that test about "a pickle write" go too.

**Docs that go stale:** `APP/CLAUDE.md:79,99,229-232,270-284,522` and `AGENTS.md`, `README.md:12,418,508`, `.cursor/rules/coding-guidelines.mdc:72-74`, `CLAUDE-reference.md:155-159,242,266`, `docs/package-guides/diskcache_reference.md`. Comments at `cache_service.py:64-96,502-517`, `scheduled_jobs_service.py:25,42,285`, `rate_limit_config.py:191`, `sponsor_inquiry_service.py:156`.

**After the port:** legacy-shape fallbacks are dead on a cold start (`rendered_cache.py:48-65`, `rss_service.py:226-233`, `pro_subscriber_service.py:126-137`), and `pro_subscriber_service.py:230` can use `iterkeys(prefix=)`.

## 7. Decisions still needed from Michael

1. **Rate-limit settings don't survive the cutover. This is the biggest risk, and it's not about the API.** Rate Limit Config holds the layer modes, the manual allowlist and the denylist (`cache_service.py:281-290`, `rate_limit_config.py:100`). "All caches are cleared at deploy" (roadmap) silently reverts enforcement to report-only and drops both lists. **Recommendation:** export before the deploy and reseed after, as an explicit cutover step, and think about whether settings belong in a cache at all.
2. **Cold-start load.** About 555 show pages share 4 regeneration slots (`show_page_cache_service.py:182-187` cites a stampede), RSS is source-of-truth in the cache, and the HTTP hash cache downloads hundreds of images per page when cold (`conftest.py:381-382`). **Recommendation:** an off-peak deploy, with a warm-up pass over the top pages.
3. **Decided (Michael, 2026-10-30): no pickle, the app adjusts** (section 5, step 3). What to know: values were read from the code and only the homepage models were run through the codec, so anything missed shows up as a `TypeError` on `set`, which `durable=True` deliberately still raises. Everything else stored is primitives, and naive datetimes, dates, tuples, sets and non-`str` dict keys round-trip exactly (tested). `timedelta` and `time` are refused. Run a real-cache pass of the test suite to shake out any others.
4. **Should the port wait for `durable=True`?** **Recommendation: yes.** Today's `DurableCache.get` swallows everything, and most `get` callers have no `try`. `on_decode_error='miss'` alone doesn't cover `Timeout`. Note that `DurableCache.set` doesn't swallow errors today (`durable_cache.py:254-256`), despite the comments at `rendered_cache.py:85` and `rss_service.py:198`, so durable is an upgrade there.
5. **Where `expire_all()` runs.** Today it's synchronous on whichever request passes the hour. `scheduled_jobs_service.py` is Memberful-specific and off outside production or with `TALKPYTHON_SCHEDULED_JOBS=0`, so there's no home yet. Separately, `transcripts_admin.py:109,128` and `mcp_service.py:69` (from `infrastructure/mcp/protocol.py:150,210,236`, `views/mcp_views.py:206`) call `expire()` on the 1 s request handle, on the event loop.
6. **How production gets the library** (section 9).

## 8. The soak

- **Current run:** `soak-results/20261030-102056` on the mini, commit `0f1b9b8`, `--hours 24 --procs 7 --threads 4 --rate 180000`. Clean at 9.3 h: about 170k ops/s, 0 timeouts, 0 stalls, 0 problems, 189/189 forks, 1 orphaned value file in chaos (expected from a kill mid-write; Phase 6a's `check()` cleans them). A local copy syncs to `soak-results/mini-20261030-102056/`.
- **Why it doesn't vouch for the final code:** plans 08 and 09 change `get_with` and `pop` after that commit, and the soak worker never calls `pop`.
- **Plan:** let it run while plan 09 is built, since it costs nothing and is the first long test of held transactions under chaos. Then stop it, keep its partial results, add the lanes (plan 09's review lists them), and run a fresh 24 hours.
- **Laptop jobs:** the problem watcher and report sync run on Michael's laptop and die if the Claude session restarts, as they did once. Restart them after a restart. They're not needed for the soak's correctness.

## 9. Getting it into production

**Nothing is installable yet.** PyPI has no `diskcache-oxide`, the version is `0.0.0`, there's no `.github/`, and the roadmap defers release wheels until "once Phase 3 is in production", which is circular. Production is documented as Python 3.14, with the architecture unverified. The dev venv is 3.15.

Options:
1. **Publish to PyPI,** as chameleon-rs does. Easiest. Add `--exclude-newer-package diskcache-oxide="1 second"` to `UV_COMPILE_FLAGS` (root `Makefile:11`), because `--exclude-newer "1 week"` would otherwise hide a fresh release.
2. **Build a Linux wheel and add it to the build context,** installed with `--find-links`. `build_pgo.sh` would need to run in `LIB/docker/linux-dev.Dockerfile`, and it's untested there.
3. **A Rust stage in the Docker build.** It needs toolchain 1.99.1 and must build from the repo root so `.cargo/config.toml` applies `LIBSQLITE3_FLAGS`; a build from an sdist may lose them (unverified). `builder-base` lives in a separate infrastructure repo, and whether it has Rust is unknown.

**Recommendation:** option 1, with a real version number and PGO wheels for Linux and macOS.

**Also check:** new value-file subdirectories are `0700` (D-015). Host-side `bin/` scripts on the bind mount, running as another UID, may be locked out of large values (inferred, not tested). Old `cache.db*` files stay on the `${TALKPYTHON_CACHE}` volume until deleted, which is useful for rollback.

## 10. Rules of the road

- **Workflow:** every plan goes in `plans/`, and `the-rust-architect` reviews each plan and each step. Small steps, committed and pushed as they land, on `main` in the library. Don't push the Talk Python branch without Michael's say-so.
- **Library checks:** `scripts/check.sh` (`--slow` before a soak) must pass on all four interpreters. Note `uv sync` removes the `docs` group, so `make docs` needs `UV_PROJECT_ENVIRONMENT=venv uv sync --locked --group docs` first. The docs site in `site/` is committed; regenerate it when the guide or API changes.
- **Testing with a second process:** a stdlib `sqlite3` connection in the test process can't stand in for one, since POSIX locks are per process. Use `tests/conformance/proc_workers.SqlitePeer`. A peer holding the write lock doesn't make `get` or `in` fail either.
- **Markdown:** never hard-wrap prose.
- **Commits:** no `Co-Authored-By` or attribution lines.
