# The FastAPI example

The [FastAPI example](https://github.com/mikeckennedy/diskcache-oxide/tree/main/examples/webapp), called Backend Notes, is a small podcast archive over a deliberately slow stand-in database. It uses diskcache-oxide's own API the way these docs recommend, and every page shows a strip of the cache reads that served it, hit or miss, and how long each took, so you can watch the cache work. Where the [Django example](django.md) shows Django's cache framework, this one shows cache-aside by hand, plus the jobs a shared cache does besides caching: sessions, rate limits, a job only one worker runs, and a work queue. This page walks through it file by file.


# Run it

With Docker, from `examples/webapp`:

``` bash
docker compose up --build
```

Or `make demo` from the repository root, which does the same and builds with a local diskcache-oxide wheel when `dist/` has one. Then open <http://127.0.0.1:8000>. Reload a page and watch the request strip turn from misses to hits. Sign in (any address works), edit an episode and watch its fragments go. Request a transcript and watch the worker take it on the Jobs page.

Without Docker, in an environment with diskcache-oxide and the packages in `requirements.txt`:

``` bash
export EXAMPLE_ROOT="$HOME/backend-notes"
gunicorn -c gunicorn.conf.py          # four web workers
python -m app.worker                  # in another terminal; start as many as you like
```


# The files

- `app/caching.py`: every cache the app opens, and `memoized`, a [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) that follows the registry.
- `app/main.py`: the app, built by `create_app()`: cache-aside fragments, invalidation, sessions, rate limits, the hourly claim, the error pages, the dashboard and the health check.
- `app/jobs.py` and `app/worker.py`: the work queue and the background worker that drains it.
- `app/cache_types.py`: the one type the app stores, registered so it never needs pickle.
- `app/store.py`: the slow stand-in database, episodes in a JSON file. Nothing about it is a recommendation.
- `app/reads.py`: the request strip. A production app doesn't need it.
- `app/logging_config.py`, `gunicorn.conf.py`, `Dockerfile`, `compose.yaml` and `app/deploy_cleanup.py`: running it.


# One directory, two kinds of cache

`app/caching.py` opens everything the app keeps on disk, once per process, under one directory:

``` python
def open_caches(root: Path) -> AppCaches:
    registry: Caches[CacheName] = Caches(root / 'cache', durable=True, timeout=1.0)
    registry.register('pages', size_limit=256 * MiB)
    registry.register('data', size_limit=64 * MiB)
    registry.register('rate_limits', size_limit=16 * MiB)

    def state(name: str) -> Cache:
        return Cache(root / 'state' / name, timeout=3.0, eviction_policy='none')

    return AppCaches(registry, state('sessions'), state('coordination'), state('jobs'))
```

Rendered fragments (`pages`), the slow loader's results (`data`) and the search rate limit's counters (`rate_limits`) can all be rebuilt, so they're in a [`Caches`](../user-guide/caches.md) registry with `durable=True`. An outage there, a lock that stays busy past `timeout=1.0` or a full disk, is a miss and a slower page, logged, never an error page. The 1 second timeout keeps a busy lock from holding a request for the default 60 seconds.

Sessions, the job claim and lock, and the work queue are state, so they're plain [Cache](../reference/Cache.md#diskcache_oxide.Cache)s under `state/`, opened directly rather than through the registry. Not durable, so a write that fails raises; never evicted for size, so nothing is dropped to make room. The comment in the code says why they skip the registry: a registry hands out a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) when a cache can't open, and on a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) [add()](../reference/Cache.add.md#diskcache_oxide.Cache.add) always wins and [push()](../reference/Cache.push.md#diskcache_oxide.Cache.push) stores nothing, which is fine for a page cache and wrong for a job claim or a queue. [Sharing a cache across web workers](../recipes/web-workers.md#the-recommended-setup) is the recipe this follows.

`gunicorn.conf.py` loads `app.main:create_app()`, a factory, so each worker builds its own app and opens its own caches.


# Cache-aside for fragments

The episode list and each episode's body are rendered once and kept in `pages`:

``` python
def fragment(key: str, tag: str, build) -> str:
    pages = registry['pages']
    html = reads.get(pages, 'pages', key)
    if html is None:
        html = build()
        pages.set(key, html, expire=FRAGMENT_TTL, tag=tag)
    return html
```

That's the cache-aside pattern from [caching expensive calls](../recipes/caching-expensive-calls.md): read, and on a miss build and store. `registry['pages']` is looked up on every call rather than held, so it follows the registry if the cache is switched off or comes back after failing to open. Every fragment expires after ten minutes (`FRAGMENT_TTL`), and each carries a tag, `episode:<n>` or `episodes`, for invalidation.


# Memoized loads

The slow loads go through `memoized`, a wrapper around the cache's [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize):

``` python
load_all = memoized(registry, 'data', expire=FRAGMENT_TTL)(_load_all)
load_episode = memoized(registry, 'data', expire=FRAGMENT_TTL)(_load_episode)
```

Like `fragment`, `memoized` looks the cache up on every call, so it follows the registry's switches ([memoize on a registered cache](../user-guide/caches.md#memoize-on-a-registered-cache)). It also records each call as a hit or a miss for the request strip.

The loader returns `Episode` dataclasses. `app/cache_types.py` registers that type once, so it's stored as its fields, without pickle ([storing your own types](../recipes/custom-types.md)):

``` python
if 'example.Episode' not in registered_types():
    register_type(Episode, name='example.Episode')
```

Every entry point imports this module before touching a cache: the web app and the worker both do, and a test checks they register the same set. A process that hasn't registered a type can't read values of it; on a durable cache they're misses with a warning, so the slow path would run every time.


# Invalidating on an edit

Saving an edit drops everything that showed the episode:

``` python
def invalidate(number: int) -> None:
    try:
        pages = registry['pages']
        pages.evict(f'episode:{number}')
        pages.evict('episodes')
        data = registry['data']
        data.delete(data.memoize(expire=FRAGMENT_TTL)(_load_episode).__cache_key__(number))
        data.delete(data.memoize(expire=FRAGMENT_TTL)(_load_all).__cache_key__())
    except Timeout, OSError:
        log.warning('could not invalidate episode %s; its cached pages expire within %s s', number, FRAGMENT_TTL)
```

`evict(tag)` removes every fragment with that tag ([invalidating groups](../recipes/invalidating-groups.md)), and [__cache_key__](../reference/Memoized.md#diskcache_oxide.Memoized.__cache_key__) gives the memoized results' keys so they can be deleted too. [evict](../reference/Cache.evict.md#diskcache_oxide.Cache.evict) raises even on a durable cache, so the call is wrapped: if invalidation fails, the stale fragments still expire within ten minutes. The editor itself always reads the store, never the cache.


# Sessions

Signing in stores a session in the `sessions` state cache, with an idle expiry that slides forward when it's half used, and an absolute limit of 12 hours from sign-in. Because the cache isn't durable, a session write that fails raises, and the app's exception handlers answer with a 503 page, not a login that doesn't stick:

``` python
@app.exception_handler(Timeout)
def busy(request: Request, exc: Timeout) -> Response:
    return render(request, 'message.html', status=503, user=None, title='Busy', message='Please try again in a moment.')
```

A second handler does the same for any other `OSError`, such as a full disk or a read-only volume, and logs it. The session also holds a CSRF token, which the edit and transcript forms carry and check.


# Rate limits that hold across workers

``` python
def allowed(request: Request, scope: str, limit: int, cache: CacheLike) -> tuple[bool, int]:
    client = request.client.host if request.client else 'unknown'
    window = int(time.time() // settings.rate_window)
    key = f'rate:{scope}:{client}:{window}'
    cache.add(key, 0, expire=settings.rate_window * 2)
    count = cache.incr(key)
    return count <= limit, max(limit - int(count), 0)
```

A counter per client per minute, shared by every worker because [add](../reference/Cache.add.md#diskcache_oxide.Cache.add) and [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) are atomic across processes ([rate limiting](../recipes/rate-limiting.md)). Which cache holds the counters decides what an outage does. Search counts in the durable `rate_limits` cache, where a failed write is answered as success, so the limit fails open: a little extra searching during an outage is fine. Sign-in counts in the `coordination` state cache, where a failed write raises and the request gets a 503, so the limit fails closed and a password guesser never gets an unlimited window. `forwarded_allow_ips` in `gunicorn.conf.py` makes `request.client.host` the real client behind a proxy.


# A job claimed by exactly one worker

Once an hour, the first request to `/` in any worker queues the sitemap job ([run once](../recipes/run-once.md)):

``` python
won = caches.coordination.add(
    f'claim:sitemap:{period}', {'pid': os.getpid(), 'at': time.time()}, expire=settings.claim_period * 2
)
```

[add](../reference/Cache.add.md#diskcache_oxide.Cache.add) stores the claim only if no one has, so exactly one worker wins the period. If the winner can't queue the job, it deletes the claim, so a later request queues it this period instead of nobody. A request uses a claim, which skips, rather than a lock, which waits.


# The work queue and the worker

`app/jobs.py` builds the queue from [work queues](../recipes/work-queues.md) on the `jobs` state cache. [push](../reference/Cache.push.md#diskcache_oxide.Cache.push) adds a job; `take` pulls one and records it as running in the same transaction, so no crash can fall between the two:

``` python
def take(jobs: Cache, worker: str) -> Optional[dict]:
    if jobs.peek(prefix=QUEUE) == (None, None):
        return None
    with jobs.transact():
        key, job = jobs.pull(prefix=QUEUE)
        if key is None:
            return None
        jobs.set(f'running:{job["id"]}', (time.time(), worker, job))
    return job
```

An idle worker only peeks, so it doesn't open a write transaction every quarter second. A job whose worker died mid-way goes back on the front of the queue after its lease (two minutes); after its third unfinished run (so two requeues) it's recorded as failed instead. A job that raises is recorded as failed at once and not retried, and the worker carries on.

`python -m app.worker` runs the loop. The sitemap job takes a [Lock](../reference/Lock.md#diskcache_oxide.Lock) while it rewrites `sitemap.xml`, so only one worker writes it at a time. A lock waits its turn, which is fine in a worker and is why locks live in workers, never in a request.


# The dashboard and the health check

`/admin/caches` shows each cache's entries and size, and for the registry's caches their hits and misses (each worker turns its hit counts on at startup, since they're per process). It answers only on loopback unless `EXAMPLE_ADMIN_ANYWHERE=1`, which `compose.yaml` sets for the demo so the host can reach it through Docker's port mapping, published on the host's loopback only. `/health/cache` is a readiness check, not a liveness one: a durable cache that's down still reports ready, since that outage is what `durable=True` absorbs, but a state cache that can't answer makes it a 503, since without its state the app can't sign anyone in or queue work ([monitoring](../recipes/monitoring.md)).


# Logging

`app/logging_config.py` is one `dictConfig` for gunicorn and the worker. The setting that matters is `disable_existing_loggers: False`: the `diskcache_oxide` logger exists from the moment the package is imported, and `dictConfig`'s default would silence it, and with it every report a durable cache makes instead of raising ([errors](../user-guide/errors.md#log-lines-when-the-library-was-told-not-to-raise)).


# Docker

`compose.yaml` runs the web app and the worker from one image with one named volume for the caches and the state. The `Dockerfile` runs as a fixed, non-root UID that must never change between image versions, since cache directories are created owner-only and a container running as another UID can't open them. `app/deploy_cleanup.py` removes an older release's files after a deploy across a format change, once every old container has stopped ([docker](../recipes/docker.md)).


# The tests

The [tests](https://github.com/mikeckennedy/diskcache-oxide/tree/main/examples/webapp/tests) cover every page, the edit-and-evict path, the rate limits, the claim, the worker and the queue's failure paths, the 503 pages, the type registrations, logging and the deploy cleanup. One starts gunicorn with four workers and two background workers as real processes and checks that the claim, the rate limit and the queue hold across all of them, with every job run exactly once. From the repository root:

``` bash
uv export --locked --only-group example --no-emit-project --no-hashes --format requirements-txt \
    | uv pip install --python venv/bin/python -r -
venv/bin/pytest -c examples/webapp/pyproject.toml examples/webapp/tests
```


# Where to go next

- [Sharing a cache across web workers](../recipes/web-workers.md), the setup this app runs.
- The [Django example](django.md), for the same ideas through Django's cache framework.
- The [recipes](../recipes/index.md), one per pattern this app puts together.
