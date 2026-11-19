# The Django example

The [Django example](https://github.com/mikeckennedy/diskcache-oxide/tree/main/examples/django) is a small Django project that runs Django's cache framework on diskcache-oxide, under gunicorn with four workers that share one cache on disk. It's deliberately small: one view per way Django uses a cache, over a slow stand-in database so you can see the cache save the time. This page walks through it file by file. The [Django cache backend](../user-guide/django.md) guide page is the reference for every setting it uses.


# Run it

From `examples/django`, in an environment with diskcache-oxide, Django and gunicorn:

``` bash
pip install 'diskcache-oxide[django]' gunicorn
export EXAMPLE_ROOT="$HOME/django-demo"
gunicorn -c gunicorn.conf.py          # four workers on 127.0.0.1:8000
```

Open <http://127.0.0.1:8000> and reload. The session counter goes up on every reload while the episode list keeps the time it was rendered at. Open an episode twice: the second visit shows the same render time, because it came from the cache. Every response carries an `X-Worker` header with the pid of the worker that answered, so `curl -i` shows different workers serving the same cached page.


# The files

- `demo/settings.py`: the two cache aliases, sessions and logging. Most of what this page is about.
- `demo/views.py`: one view per way Django uses a cache.
- `demo/catalog.py`: the slow stand-in database. Every load sleeps `EXAMPLE_LOAD_DELAY` seconds (0.2 by default) and is counted, so the tests can see the cache work.
- `demo/wsgi.py` and `gunicorn.conf.py`: loading the app in gunicorn's master and forking four workers.
- `demo/middleware.py`: the `X-Worker` and `X-Warmed-In` headers.
- `tests/`: the views through Django's test client, then gunicorn with four real workers.


# Two aliases: one for pages, one for state

The first decision is the one that matters most. Pages and fragments are cache: if they're lost, the next request rebuilds them. Sessions and counters are state: if a write is lost, a visitor is logged out or a count is wrong. They need opposite settings, so they get two aliases in two directories ([state isn't cache](../user-guide/caches.md#state-isnt-cache) explains why):

``` python
ROOT = Path(os.environ.get('EXAMPLE_ROOT', '/var/lib/django-demo'))

CACHES = {
    'default': {
        'BACKEND': 'diskcache_oxide.DjangoCache',
        'LOCATION': str(ROOT / 'cache' / 'pages'),
        'TIMEOUT': 600,
        'OPTIONS': {'durable': True, 'size_limit': 512 * 2**20},
    },
    'state': {
        'BACKEND': 'diskcache_oxide.DjangoCache',
        'LOCATION': str(ROOT / 'state' / 'site'),
        'TIMEOUT': None,
        'OPTIONS': {'eviction_policy': 'none'},
    },
}
```

`default` is durable. A write that can't get the lock within the 1 second lock wait is dropped and logged, and a read that fails is a miss, so an outage on the page cache is a slower page, never an error page. It's capped at 512 MiB and evicts the least recently stored entries past that.

`state` is not durable, so a write that fails raises: a login that didn't stick is an error you see, not a surprise. It never evicts for size (`eviction_policy: 'none'`), since evicting a session to make room logs someone out. With `TIMEOUT: None`, entries stay until they expire on their own (sessions after Django's `SESSION_COOKIE_AGE`, two weeks by default) or are deleted.

The layout keeps them apart on disk too: `cache/` holds only what can be rebuilt, so a deploy can delete it safely, and `state/` must never go with it.


# Sessions on the state alias

``` python
SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'state'
```

Django's cache session engine keeps each session in the cache, so a visitor's session follows them whichever worker answers. It needs no database and no sessions app, which is why `INSTALLED_APPS` holds only `demo` and `DATABASES` is empty.

[DjangoCache](../reference/DjangoCache.md#diskcache_oxide.DjangoCache) refuses to store sessions on a durable alias: point `SESSION_CACHE_ALIAS` at `default` and the first use raises `ImproperlyConfigured`. A dropped session write would look to Django's session engine like a race to retry, or would only be logged, and the visitor's login would quietly not stick. A test in the example checks the refusal.

This demo makes a session for every visitor to `/`, to show the counter. A real site shouldn't make sessions for anonymous visitors, since a `state` alias that never evicts grows with every one.


# The per-view cache

``` python
@cache_page(60)
def episode(request: HttpRequest, number: int) -> HttpResponse:
    if number not in catalog.EPISODES:
        raise Http404('no such episode')
    return render(request, 'demo/episode.html', {'episode': catalog.load_episode(number), 'rendered_at': time.time()})
```

`cache_page` stores the whole response, pickled, in the `default` alias for 60 seconds, and every worker serves it from there. A 404 isn't cached.

The view doesn't read the session, and that matters. `SessionMiddleware` adds `Vary: Cookie` to a response whose view touched the session, but only after `cache_page` has already stored it, so the stored copy doesn't vary on the cookie and one visitor's page would be served to everyone. A `cache_page` view that must read the session needs `@vary_on_cookie` as well. The per-view cache keys also include the `Host` header, which is why `ALLOWED_HOSTS` names real hosts: with `'*'`, anyone could fill the page cache with copies under made-up hosts and push the real pages out.


# A template fragment

The episode list on the home page is a `{% cache %}` block in `home.html`:

``` django
{% cache 600 episode_list %}
<p>Episode list rendered at {{ rendered_at|floatformat:3 }} (cached for ten minutes).</p>
<ul id="episodes" data-rendered-at="{{ rendered_at }}">
  {% for episode in episodes %}
  <li>[{{ episode.title }}](/episode/{{ episode.number }})</li>
  {% endfor %}
</ul>
{% endcache %}
```

The view passes `catalog.episode_list` itself, not its result. Django templates call a callable when they use it, so the slow load runs only when the fragment misses. The rest of the page, the session counter included, renders on every request.


# The low-level API

``` python
def stats(request: HttpRequest) -> JsonResponse:
    return JsonResponse(cache.get_or_set('stats', catalog.compute_stats, 300))


def visits(request: HttpRequest) -> JsonResponse:
    state = caches['state']
    state.add('visits', 0)  # only the first one wins
    return JsonResponse({'visits': state.incr('visits'), 'pid': os.getpid()})
```

`get_or_set` is cache-aside in one call: it returns the cached value or computes, stores and returns it. Two workers that miss at once both compute; the cache doesn't hold one back.

The visit counter is state, so it lives on `state`. [add](../reference/Cache.add.md#diskcache_oxide.Cache.add) stores 0 only if the key isn't there yet, and [incr](../reference/Cache.incr.md#diskcache_oxide.Cache.incr) is atomic across processes, so with four workers taking requests at once every count is handed out exactly once. On Django 5.2 and 6.0, Django's own [aincr](../reference/DjangoCache.aincr.md#diskcache_oxide.DjangoCache.aincr) is a get and then a set; [DjangoCache](../reference/DjangoCache.md#diskcache_oxide.DjangoCache) overrides it, so the async form is atomic too.


# gunicorn, forking and the warm cache

``` python
# gunicorn.conf.py
wsgi_app = 'demo.wsgi:application'
workers = int(os.environ.get('WEB_WORKERS', '4'))
preload_app = True
timeout = 30
```

With `preload_app`, gunicorn's master imports the app once and then forks the workers, which inherit everything it loaded. `demo/wsgi.py` touches the cache before that fork:

``` python
application = get_wsgi_application()

from django.core.cache import caches

import demo

for alias in caches.all():
    alias.get('warm-up')
demo.warmed_in = os.getpid()
```

Opening a cache before forking is safe: every worker keeps using the handle it inherited ([concurrency and forks](../user-guide/concurrency-and-forks.md) covers why). It isn't required; opening after the fork works just as well. It's here because an app that warms its cache at import does exactly this, and because touching every alias builds each one, so a misconfigured one, such as a durable session alias, fails when the server starts rather than on the first request that uses it.

`timeout = 30` is gunicorn's worker timeout. It's well past the cache's 1 second lock wait, so a write stuck behind a held lock raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) long before gunicorn would kill the worker.


# Seeing which worker answered

`demo/middleware.py` adds two headers to every response: `X-Worker`, the pid of the worker that answered, and `X-Warmed-In`, the pid of the process that warmed the cache, which under `preload_app` is the master. The middleware sits outside `cache_page`, so the headers are set on every response, a cached one included, and are never stored with it.


# Logging

``` python
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'loggers': {
        'diskcache_oxide': {'handlers': ['console'], 'level': 'WARNING'},
        'django.request': {'handlers': ['console'], 'level': 'ERROR'},
    },
}
```

The `diskcache_oxide` logger is where a durable alias reports what it absorbed: a dropped write, a failed read, a directory that couldn't open. Keep it, or the durable alias's outages are silent. `django.request` is there because with `DEBUG` off, Django sends a 500's traceback only to `mail_admins`, and a failed write on `state` (which raises, by design) should show up in the log too.


# The tests

The [tests](https://github.com/mikeckennedy/diskcache-oxide/tree/main/examples/django/tests) come in two parts. `test_pages.py` runs each view through Django's test client: the per-view cache serves the stored response, a missing episode is a 404 and isn't cached, the fragment renders once, the session lands in the `state` alias, `get_or_set` computes once, `manage.py check` passes, and a durable session alias is refused.

`test_processes.py` starts gunicorn with four workers as real processes and waits until all four have answered, so no test runs on fewer workers than it claims. Then, checking by the `X-Worker` header that more than one worker took part in each:

- 200 concurrent requests to `/visits` get every count from 1 to 200 exactly once.
- A session follows its visitor across workers, counting up without a gap.
- Every worker serves the same cached page, the same fragment and the same `get_or_set` result.
- Every worker reports the same `X-Warmed-In` pid, and it isn't one of theirs: the master opened the cache before forking.

To run them from the repository root:

``` bash
for group in django example; do
    uv export --locked --only-group "$group" --no-emit-project --no-hashes --format requirements-txt \
        | uv pip install --python venv/bin/python -r -
done
venv/bin/pytest -c examples/django/pyproject.toml examples/django/tests
```


# Where to go next

- The [Django cache backend](../user-guide/django.md) guide page: every setting, what's refused and why, errors and what [durable](../reference/Cache.durable.md#diskcache_oxide.Cache.durable) covers, and coming from diskcache's backend.
- The [FastAPI example](fastapi.md), which does the same jobs with diskcache-oxide's own API, plus rate limits, a work queue and a job only one worker runs.
- [Sharing a cache across web workers](../recipes/web-workers.md), the production setup this example follows.
