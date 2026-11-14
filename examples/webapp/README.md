# Backend Notes: a diskcache-oxide example app

A small FastAPI app that uses diskcache-oxide the way its docs recommend: a podcast archive over a deliberately slow stand-in database, so you can watch the cache work. Every page shows which cache reads served it, hit or miss, and how long each took.

## What it shows

Each piece points at the page of the docs that explains it.

- **A durable registry for rebuildable data** ([web workers](https://mkennedy.codes/docs/diskcache-oxide/recipes/web-workers.html#the-recommended-setup)). `pages` holds rendered HTML fragments, `data` the slow loader's results through `memoize`, and `rate_limits` per-client counters, all under `/var/lib/my-app/cache`, `durable=True`, `timeout=1.0`. An outage there is a miss and a slower page, never an error page. See `app/caching.py`.
- **State as plain caches, opened directly** ([state isn't cache](https://mkennedy.codes/docs/diskcache-oxide/user-guide/caches.html#state-isnt-cache)). Sessions, the job claim and lock, and the work queue live under `state/`, not durable, with eviction off. A write that fails raises, and the app answers with a 503 page instead of a login that doesn't stick.
- **Tagged fragments and invalidation** ([invalidating groups](https://mkennedy.codes/docs/diskcache-oxide/recipes/invalidating-groups.html)). Saving an edit runs `pages.evict('episode:<n>')` and deletes the loader's memoized results; every fragment also expires within ten minutes, in case an eviction fails.
- **`memoize` on a registered cache** ([caches](https://mkennedy.codes/docs/diskcache-oxide/user-guide/caches.html#memoize-on-a-registered-cache)), looked up on every call so it follows the registry's switches.
- **Your own types without pickle** ([custom types](https://mkennedy.codes/docs/diskcache-oxide/recipes/custom-types.html)). `app/cache_types.py` registers the `Episode` dataclass, and every entry point imports it; a test checks the web app and the worker agree.
- **A rate limit that holds across workers** ([rate limiting](https://mkennedy.codes/docs/diskcache-oxide/recipes/rate-limiting.html)), proxy-aware through `forwarded_allow_ips`. Search counts in the durable registry and fails open during an outage; sign-in counts in a state cache and fails closed, with a 503, so a password guesser never gets an unlimited window.
- **An hourly job claimed by exactly one worker** ([run once](https://mkennedy.codes/docs/diskcache-oxide/recipes/run-once.html)) with `add()`, which queues the sitemap job.
- **A work queue with a crash-safe running record** ([work queues](https://mkennedy.codes/docs/diskcache-oxide/recipes/work-queues.html)): a job its worker died on goes back on the queue, three times at most, and a job that raises is recorded as failed while the worker carries on. The worker (never a request) holds a `Lock` while it rewrites `sitemap.xml`.
- **A cache dashboard and a readiness check** ([monitoring](https://mkennedy.codes/docs/diskcache-oxide/recipes/monitoring.html)): `/admin/caches` (loopback only) and `/health/cache`.
- **Logging that reaches you** ([errors](https://mkennedy.codes/docs/diskcache-oxide/user-guide/errors.html#log-lines-when-the-library-was-told-not-to-raise)): one `dictConfig` for gunicorn and the worker that keeps the `diskcache_oxide` logger alive.
- **Docker and Compose** ([docker](https://mkennedy.codes/docs/diskcache-oxide/recipes/docker.html)): one named volume, a fixed non-root UID, and `app/deploy_cleanup.py` for after a format change.

## Run it

With Docker:

```bash
cd examples/webapp
docker compose up --build
```

Then open <http://127.0.0.1:8000>. Reload a page and watch the request strip turn from misses to hits; sign in (any address works), edit an episode and watch its fragments go; request a transcript and watch the worker take it on the Jobs page.

Without Docker, from this directory, in an environment with diskcache-oxide and the packages in `requirements.txt`:

```bash
export EXAMPLE_ROOT="$HOME/backend-notes"
gunicorn -c gunicorn.conf.py          # four web workers
python -m app.worker                  # in another terminal; start as many as you like
```

`uvicorn --factory app.main:create_app --reload` runs a single worker for development.

Settings come from the environment: `EXAMPLE_ROOT` (default `/var/lib/my-app`), `EXAMPLE_LOAD_DELAY` (how slow the stand-in database is, 0.4 s), `EXAMPLE_SECURE_COOKIES=1` (mark the session cookie HTTPS-only: set it wherever the app is served over HTTPS), `WEB_WORKERS`, `BIND`, `FORWARDED_ALLOW_IPS` (addresses or CIDR ranges). diskcache-oxide itself reads no environment variables.

## Test it

From the repository root, with the `example` dependency group installed:

```bash
uv export --locked --only-group example --no-emit-project --no-hashes --format requirements-txt \
    | uv pip install --python venv/bin/python -r -
venv/bin/pytest -c examples/webapp/pyproject.toml examples/webapp/tests
```

The tests cover every page, the edit-and-evict path, the rate limits, the claim, the worker and the queue's failure paths, the 503 pages, the type registrations, logging, and the deploy cleanup, plus one that starts gunicorn with four workers and two background workers as real processes and checks that the claim, the rate limit and the queue hold across all of them, with every job run exactly once. `scripts/check.sh` runs them on 3.14 and 3.14t, and `scripts/example-docker.sh` smoke-tests the image.
