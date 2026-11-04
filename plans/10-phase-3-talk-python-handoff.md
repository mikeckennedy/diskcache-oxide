# Phase 3 handoff: moving Talk Python to diskcache-oxide

Where things stood on 2026-10-30, with the library's status (sections 1, 3 and 8) updated on 2026-11-03, and everything needed to pick up the port without the conversation that produced it. Read sections 1-3 to get oriented, and use the rest as reference while working.

Paths: `LIB` = `~/github/mk/diskcache-oxide`. `APP` = `~/github/talk-python/talk-python-to-me/src/talk_python_to_me_com`. `PKG` = `APP/talk_python_to_me_com`. Line numbers were read on 2026-10-30 and will drift. The Talk Python branch is `diskcache-oxide-cache`, and **nothing has been changed on it yet**.

## 1. Status

| Piece | State |
|---|---|
| Core `Cache`, thread-, process- and fork-safe | Done (Phases 2, 2.5). Gates are GO on macOS and Linux, 3.14 and 3.14t |
| Plan 08: `on_decode_error`, `ForeignValueError`, per-call `timeout=`, `Caches` | Done, committed, architect-reviewed |
| Plan 09 step 1: `pop` keeps values it can't read | Built and reviewed (`a79f895`; step 1b, timeouts behind a pending fork, `58508b8`) |
| Plan 09 step 2: `Cache(durable=True)` | Built (`9751682`; step 2b, forced-checkpoint retries, `5b2ea85`). The deep review of `5b2ea85` found four bugs, all fixed (`08e00f2`, `81aa84d`, `ae48524`) |
| Plan 09 step 3: `pop` in the soak kit, then a fresh 24-hour run | Done. The kit has the plan 09 lanes (`08e00f2`) |
| 24-hour soak on the mini | **PASS on `ae48524`** (section 8). Since then: comments, docs, tests and formatting, plus the finding 3 follow-up to the deferred-owner lock and its child-hook refinement (a forked child frees a holder that held only the stamp), paths the soak didn't reach |
| PGO wheels | Rebuilt 2026-11-04 on `07ed13d` plus the child-hook refinement into `LIB/dist/`, macOS arm64 only (section 4) |
| Linux wheel | **Doesn't exist.** Production installs from a wheel, not PyPI (Michael). Version is still `0.0.0`. Blocks production, not local work |
| Port plan | **Not written.** Section 6 has the raw material; it goes in `plans/` and gets the architect's review |

Plan documents: `plans/00-roadmap.md` (Phase 3), `plans/06` (the concurrency work), `plans/08` (what's built, with the reasoning), `plans/09` (built, including the review's required changes). `reviews/deep-review-5b2ea85-2026-10-31.md` has the deep review of plan 09 and the status of each finding.

## 2. Decisions Michael has made

- **`Cache(durable=True)` is the design:** "durable means DURABLE as much as we can deliver". On a single-key call, any operational failure (timeout, disk full, I/O, SQLite error, a forked child that can't write) is reported and answered as a miss or False. `on_decode_error` defaults to `'miss'`. Admin calls (`clear`, `expire`, `cull`, `checkpoint`, `len`, `volume`, iteration), bugs, and calls inside a hold on that store still raise.
- **`delete` returns False under `durable=True`.** The risk is accepted: a lost invalidation serves stale data, and the log line is the signal.
- **`ForeignValueError`** keeps its name.
- **`pop` gets fixed** so it doesn't destroy a value it can't read.
- **A failed open is retried every 10 s** (`retry_open_after=10.0`).
- **Self-healing reads are a constructor option, and the registry is called `Caches`.** Purposes, the settings file, Sentry and logbook stay in the app.
- **The soak restarts once plan 09 is built:** a fresh 24 hours on that commit, replacing the 6-8 hour re-soak. Done: it passed on `ae48524`. The mini is Michael's to give; it's currently dedicated to this.
- **No pickle in Talk Python: msgpack only, and the app adjusts.** The two dataclass values become plain dicts (section 5, step 3). No cache opens with `pickle=True`.
- **Use `ty`, never mypy,** in the library too.

## 3. What to do next, in order

1. **Michael's perf test** (section 5), against the wheels already in `dist/`. It needs a small spike on the branch, because the app can't use the library unmodified.
2. **Done: build plan 09** in the library, with the required changes in its review section. Steps 1, 1b, 2 and 2b are committed, and the deep review's four findings are fixed.
3. **Done: add `pop` and durable lanes to the soak kit and soak 24 hours.** It passed on `ae48524` (section 8). Rule for any rerun: one soak at a time on the mini. Use `zsh -lc` over ssh, and see the memory note `soak-on-mini`.
4. **Write the port plan** from section 6, settling section 7's decisions with Michael. The architect reviews it.
5. **Port on the branch.** Run `./venv/bin/ty check`, `./venv/bin/ruff check --fix talk_python_to_me_com/`, `./venv/bin/ruff format talk_python_to_me_com/` and the tests, from `APP`. About 8,500 tests, serial, and `tests/test_type_check.py` fails on any `ty` diagnostic.
6. **Before production:** the soak PASS on the final library commit, a Linux wheel (section 9), and the cold-cutover steps (section 7, items 1 and 2). Near the end, Michael runs a real test on `talk-python-htz`, on real Linux, as the near-final check (2026-10-30). That's where Linux performance gets measured, including the fork cost at the worker's real size (section 11).

Library changes during the port: Python-only changes such as `Caches` shouldn't need another soak. A change to `get_with`, `pop` or the locking does.

## 4. The PGO wheels

`scripts/build_pgo.sh [python ...]` (default 3.14, 3.14t, 3.15, 3.15t) builds instrumented, trains on `bench.baseline --quick` plus `bench/pgo_train.py`, merges the profiles, and rebuilds. Each interpreter gets its own profile, since GIL and free-threaded builds compile different code. In `LIB/dist/`:

```
diskcache_oxide-0.0.0-cp314-cp314-macosx_11_0_arm64.whl
diskcache_oxide-0.0.0-cp314-cp314t-macosx_11_0_arm64.whl
diskcache_oxide-0.0.0-cp315-cp315-macosx_11_0_arm64.whl     <- Talk Python's dev venv (3.15)
diskcache_oxide-0.0.0-cp315-cp315t-macosx_11_0_arm64.whl
```

- **Contents:** the commit after `07ed13d`, with the child-hook refinement in `forget_inherited_owners` (rebuilt 2026-11-04 from its working tree), so plan 09, every deep-review fix and the refinement are in. `dist/` is git-ignored. Rebuild after any library change the perf test should see.
- **Verified (2026-11-04):** each wheel is free of instrumentation (no `__llvm_profile` symbols), links the bundled SQLite 3.54.1 with the shipped flags, and passes the full suite and conformance tests from a fresh venv: 1098 passed on each, 320 conformance on the GIL builds and 322 on the free-threaded ones. The fresh venv needs `ty` too, for `test_ty_sees_both_classes_as_cache_like`.
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
   def doc(x):
       return to_doc(x, include_id=True) if x is not None else None


   home_cache.set(
       SNAPSHOT_KEY,
       {
           'latest_episode': doc(self.latest_episode),
           'latest_guests': [doc(g) for g in self.latest_guests],
           'reviews': [doc(r) for r in self.reviews],
           'upcoming_episodes': [doc(e) for e in self.upcoming_episodes],
           'upcoming_guest_lookup': {k: doc(g) for k, g in lookup.items()},
       },
       expire=SNAPSHOT_TTL_SECONDS,
   )

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
4. **Should the port wait for `durable=True`?** **Settled: it's built** (`9751682`, 2026-10-31), so the port doesn't wait. The original recommendation was yes: Today's `DurableCache.get` swallows everything, and most `get` callers have no `try`. `on_decode_error='miss'` alone doesn't cover `Timeout`. Note that `DurableCache.set` doesn't swallow errors today (`durable_cache.py:254-256`), despite the comments at `rendered_cache.py:85` and `rss_service.py:198`, so durable is an upgrade there.
5. **Where `expire_all()` runs.** Today it's synchronous on whichever request passes the hour. `scheduled_jobs_service.py` is Memberful-specific and off outside production or with `TALKPYTHON_SCHEDULED_JOBS=0`, so there's no home yet. Separately, `transcripts_admin.py:109,128` and `mcp_service.py:69` (from `infrastructure/mcp/protocol.py:150,210,236`, `views/mcp_views.py:206`) call `expire()` on the 1 s request handle, on the event loop.

## 8. The soak

- **Last run: PASS.** `soak-results/20261031-230228` on the mini, commit `ae48524`, `--hours 24 --procs 7 --threads 4 --rate 180000`, with the plan 09 lanes. 0 problems, 0 stalls, 0 timeouts, no leak or drift flags. 494 kills, 140,999 forks with 0 failures. Worst RSS slope 0.10 MiB/h; get p99 86 µs in the first hour, 90 µs in the last.
- **Plan 09 lanes:** exactly-once `pop`: 2,002,255 tokens, 0 duplicates, 1 lost and excused by a kill. `durable=True`: 113.5M calls, 385,261 answered as failures and checked, 0 exceptions escaped, 0 broken. 33.7M `pop` calls across all four sites. Forced checkpoints on the two one-writer auto stores: 287,594 of 287,608 rounds succeeded (5 busy, 9 stopped for a pending fork). Orphans: 3 files in the ledger store, expected from kills mid-write (Phase 6a's `check()` cleans them).
- **What it vouches for:** the library through `ae48524`. Commits after it through `9458d00` change comments, docs, tests and formatting only. The finding 3 follow-up after that changes how a deferral behind a stuck fork takes the deferred-owner lock (it waits for a busy holder in its own process instead of leaking), and the commit after it narrows the child's after-fork hook so a holder that held only the stamp is freed rather than marked lost. The soak never reached that path (next point), so the core tests vouch for it, not the soak. Rerun the soak if `get_with`, `pop` or the gate's locking changes (section 3).
- **What it doesn't cover:** the deep review's finding 3 (a deferred release that has to wait for a stuck fork) was almost certainly never reached. The longest stuck fork was 0.727 s against the 1 s release budget, and no counter records a deferral. The core tests in `store/fork_timeout.rs` cover it.
- **Previous run:** `soak-results/20261030-102056` on `0f1b9b8`, before plan 09, was stopped and replaced by this one. A local copy is in `soak-results/mini-20261030-102056/`.
- **Laptop jobs:** the problem watcher and report sync run on Michael's laptop and die if the Claude session restarts, as they did once. Restart them after a restart. They're not needed for the soak's correctness.

## 9. Getting it into production

**Decided (Michael, 2026-10-30): install from a wheel, not PyPI, for now.** The wheel goes in the Docker build context and installs with `--find-links`. PyPI can come later.

What that needs:
- **A Linux wheel, which doesn't exist yet.** `dist/` is macOS only. Production is documented as Python 3.14, so it's `cp314` for production's architecture, which is unverified: check it before building. `scripts/build_pgo.sh` would need to run inside `LIB/docker/linux-dev.Dockerfile` (or a manylinux image, so the wheel's glibc requirement isn't newer than the production image's), and that's untested.
- **A version.** The wheel is still `0.0.0`. A real version number makes it clear which build is deployed; `viewmodels/admin/server_info_viewmodel.py:30` lists package versions on the admin page.
- **The Docker build:** requirements install at `etc/docker/Dockerfile:26-28`, and the runtime stage copies `/venv`, so the compiled extension carries over. The COPY paths imply a parent build context, so the wheel needs to live inside it. `requirements.piptools` and `requirements.txt` swap `diskcache==5.6.3` for the wheel. `builder-base` lives in a separate infrastructure repo.

**Also check:** new value-file subdirectories are `0700` (D-015). Host-side `bin/` scripts on the bind mount, running as another UID, may be locked out of large values (inferred, not tested). Old `cache.db*` files stay on the `${TALKPYTHON_CACHE}` volume until deleted, which is useful for rollback.

## 10. Rules of the road

- **Workflow:** every plan goes in `plans/`, and `the-rust-architect` reviews each plan and each step. Small steps, committed and pushed as they land, on `main` in the library. Don't push the Talk Python branch without Michael's say-so.
- **Library checks:** `scripts/check.sh` (`--slow` before a soak) must pass on all four interpreters. Note `uv sync` removes the `docs` group, so `make docs` needs `UV_PROJECT_ENVIRONMENT=venv uv sync --locked --group docs` first. The docs site in `site/` is committed; regenerate it when the guide or API changes.
- **Testing with a second process:** a stdlib `sqlite3` connection in the test process can't stand in for one, since POSIX locks are per process. Use `tests/conformance/proc_workers.SqlitePeer`. A peer holding the write lock doesn't make `get` or `in` fail either.
- **Markdown:** never hard-wrap prose.
- **Commits:** no `Co-Authored-By` or attribution lines.
- **Type checking in the library:** `ty check` in `scripts/check.sh`, and `tests/unit/test_stubs.py` checks `_core.pyi` against the compiled module (what `mypy.stubtest` did). `ty` also found that `CacheLike`'s `__contains__` and friends allowed a keyword `key` where `Cache` doesn't; they're positional-only now.
- **Where results come from (Michael, 2026-10-30):** run natively on the Mac by default. Production is Linux in Docker on real Linux. Every "Linux" result so far (the gates, the 6-hour soak, `linux-check.sh`) came from the OrbStack VM, so it shows the code works on Linux, not how it performs. Production performance claims need real Linux hardware. Don't run the VM checks routinely: only when a specific Linux behavior needs testing.

## 11. The deep review's §6.11–13: its last open problems

Settled with Michael on 2026-11-03, then tightened after the architect's review.

**Fork cost at production scale: measured on `talk-python-htz`, as a production gate.**

- **What's known:** forks have only been timed in small processes. Quiet on macOS, about 1.1-1.3 ms p50 (1,108 µs on 3.14, 1,334 µs on 3.14t). Beside load on Linux 3.15t, about 3 ms p50, 11-22 ms p99 and up to 320 ms at worst (plan 09), but those came from the OrbStack VM, so they show the shape, not production timing. A fork copies the parent's page tables, so it costs more in a large worker. Beside load it also waits for every call in flight (`begin_fork`), and every cache call in the process waits for the fork within its own `timeout`, a budget it shares with its SQLite busy wait (`Caches(timeout=1.0)` in the port).
- **First, check whether it's moot.** Grep the app for `os.fork`, `multiprocessing` with the `fork` context, `ProcessPoolExecutor` and `preexec_fn`. Plain `subprocess` doesn't run the at-fork hooks, and 3.14's default multiprocessing start method on Linux is `forkserver`. Then count real forks for a day: the library keeps no fork counter (`_gate_stats()` has only `pending`, `holders`, `parked`, `fork_timeouts` and `deferred`), so register an `os.register_at_fork(after_in_parent=...)` hook that counts and logs them, inside each worker (after granian starts it), or it counts nothing. If no worker forks with a cache open, record that, and the gate doesn't apply.
- **Otherwise, measure inside the production container**, not on the bare host, since memory cgroups and transparent huge pages change fork cost. Take `RssAnon` from a real worker's `/proc/<pid>/status` and its number of open caches, then run `python -m bench.fork_cost --rss-mib <RssAnon in MiB> --caches <n> --load-threads 4`. `--rss-mib` is a target the process grows to, and `--load-threads` times other threads' cache calls across the forks.
- **The gate:** across the run, the worst fork, and the worst load-thread call around a fork, stay under 100 ms (a tenth of the 1 s timeout), and `_gate_stats()` shows `fork_timeouts == 0`. Record how often production forks too, since a rare 100 ms stall and a constant one aren't the same cost.
- **What the bench doesn't model:** a child that lives and makes the parent pay real copy-on-write copies (its child exits at once), and the write faults the parent takes on its next requests as refcounts touch pages the fork write-protected.

**The two historical unexplained failures.**

- **The Linux 3.15t flake in `test_auto_mode_shrinks_the_wal_back_after_a_burst_with_readers_in_other_processes`: closed, explained and fixed.** It was a real defect, forced TRUNCATE checkpoints reporting busy beside readers in other processes, fixed by plan 09 step 2b's `truncate_tries`. The pinned test then passed 30 of 30 on all four Linux builds and the review approved it. Two caveats: those runs were in the OrbStack VM, so they count for correctness, not timing, and the GIL builds force few checkpoints there, so their 30 of 30 says little about the fix. The free-threaded builds carry the evidence.
- **The unit failure committed with `8617f26` (2026-10-29, 3.14 GIL, `venv`).** The test's name wasn't kept, and 5 reruns right after passed. A repeat loop can't prove it's gone, only fail to reproduce it. The rule: the unit suite on the Mac, repeated for 3 hours in `venv` (3.14 GIL, where it failed) and then 1.5 hours in `.venv-ft` (3.14t), keeping the full output of any failure. With no failures, it's recorded as not reproduced and closed. With one, it gets a name and a fix.
  - Result: not reproduced, closed (2026-11-04). `venv`: 173 runs from 22:57 to 01:59, all passing (838 passed, 1 skipped each), on the extension built at 22:43 from `07ed13d`. `.venv-ft`: 87 runs from 01:59 to 03:30, all passing (836 passed, 3 skipped each), on an extension rebuilt with the child-hook refinement. That's 260 runs of the suite with no failure. Some `venv` runs overlapped other work on the machine (core tests, bench smoke runs, `cargo doc`), which only added load.

**The long-run memory slope: not a blocker, watched in production.**

- The worst per-worker fit across both 24 h soaks is +0.35 MiB/h, 6x under the soak's 2 MiB/h leak flag (`RSS_LEAK_MIB_PER_H`, `soak/report.py`). But that flag is loose: a real 1 MiB/h leak, 24 MiB a day, would pass it.
- The same role grew in both runs: the long-lived 3.14t worker 1 went 79.7 to 85.1 MiB on 2026-10-30 and 90.1 to 94.5 MiB on 2026-10-31, about +5 MiB a day each time, while the other three workers in the second run fell. That fits an allocator settling, but a linear fit over 24 h can't tell settling from slow growth, and nothing in the summaries shows it flattening.
- If it's linear, it's bounded at about 8 MiB a day per worker. Watch worker RSS on htz over 7 days or more (moot if workers are recycled sooner).
