# Django on diskcache-oxide: an example project

A small Django project that runs Django's cache framework on diskcache-oxide's `DjangoCache`, under gunicorn with four workers that share one cache on disk. It's deliberately small: one view per way Django uses a cache, over a slow stand-in database so you can see the cache work.

## What it shows

- **Two aliases, one for pages and one for state** ([state isn't cache](https://mkennedy.codes/docs/diskcache-oxide/user-guide/caches.html#state-isnt-cache)). `default` holds pages and fragments under `$EXAMPLE_ROOT/cache/` with `durable=True`, so an outage there is a slower page, never an error page. `state` holds sessions and counters under `$EXAMPLE_ROOT/state/`, without `durable`, so a write that fails raises (and is logged) instead of a login that doesn't stick. It never evicts for size, so it grows until entries expire: this demo makes a session for every visitor to `/` to show it working, which a real site shouldn't do for anonymous visitors. See `demo/settings.py`. `DjangoCache` refuses a durable alias for sessions, and a test shows it.
- **The per-view cache.** `/episode/<n>` is wrapped in `@cache_page(60)`: the whole response is pickled into the cache and served from it by every worker. It doesn't read the session, which a `cache_page` view mustn't do without `@vary_on_cookie` (the comment in `demo/views.py` says why).
- **A template fragment.** The episode list on `/` is in a `{% cache %}` block, rendered once and served from the cache for ten minutes.
- **Cached sessions.** `SESSION_ENGINE` is Django's cache engine on the `state` alias, so a visitor's session follows them whichever worker answers.
- **The low-level API.** `/stats` uses `cache.get_or_set`, and `/visits` an `add` and an `incr` that every worker shares: `incr` is atomic across processes.
- **gunicorn with `preload_app`** ([web workers](https://mkennedy.codes/docs/diskcache-oxide/recipes/web-workers.html)). The master loads Django and touches the cache before it forks (`demo/wsgi.py`), and every worker keeps using the handle it inherited.

## Run it

From this directory, in an environment with diskcache-oxide, Django and gunicorn (`pip install 'diskcache-oxide[django]' gunicorn`):

```bash
export EXAMPLE_ROOT="$HOME/django-demo"
gunicorn -c gunicorn.conf.py          # four workers on 127.0.0.1:8000
```

Then open <http://127.0.0.1:8000>. Reload the home page and the session counter goes up while the episode list keeps the time it was rendered at; open an episode twice and the second shows the same render time, served from the cache. Every response carries an `X-Worker` header with the pid of the worker that answered (`curl -i`), so you can watch different workers serve the same cached page, and `X-Warmed-In`, the master that opened the cache before forking. `python manage.py runserver` runs a single process for development.

Settings come from the environment: `EXAMPLE_ROOT` (default `/var/lib/django-demo`), `EXAMPLE_LOAD_DELAY` (how slow the stand-in database is, 0.2 s), `EXAMPLE_SECURE_COOKIES=1` (mark the session cookie HTTPS-only), `WEB_WORKERS`, `BIND`, `ALLOWED_HOSTS` (comma-separated, default `127.0.0.1,localhost`), `DJANGO_SECRET_KEY` and `DJANGO_DEBUG`. diskcache-oxide itself reads no environment variables.

## Test it

From the repository root, with the `django` and `example` dependency groups installed:

```bash
for group in django example; do
    uv export --locked --only-group "$group" --no-emit-project --no-hashes --format requirements-txt \
        | uv pip install --python venv/bin/python -r -
done
venv/bin/pytest -c examples/django/pyproject.toml examples/django/tests
```

The tests cover each view through Django's test client, then start gunicorn with four workers as real processes (waiting until all four have answered) and check, by the `X-Worker` header, that more than one worker took part in each of these: a counter shared by 200 concurrent requests hands out every number exactly once, a session follows its visitor across workers, every worker serves the same cached page, fragment and `get_or_set` result, and the workers were forked from a master that had already opened the cache. `scripts/check.sh` runs them on 3.14 and 3.14t.
