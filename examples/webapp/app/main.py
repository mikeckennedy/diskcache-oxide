"""The web app, built by `create_app()`. Run it with `gunicorn -c gunicorn.conf.py`, or for development
`uvicorn --factory app.main:create_app --reload`.

Every route is a plain `def`, so FastAPI runs it in a worker thread: cache writes never run on the
event loop (see the asyncio recipe). Reads would be fine on the loop; keeping one style is simpler.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from diskcache_oxide import CacheLike, Lock, Timeout, __version__
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import jobs, reads
from app.caching import AppCaches, memoized, open_caches
from app.settings import Settings
from app.store import Episode, Store

log = logging.getLogger('app')
HERE = Path(__file__).parent
FRAGMENT_TTL = 600  # every cached fragment also expires, so a failed invalidation can't last long
IDLE = 30 * 60  # a session ends after 30 minutes without a request
ABSOLUTE = 12 * 3600  # and 12 hours after sign-in, however active


def human_size(n: float) -> str:
    for unit in ('B', 'KiB', 'MiB'):
        if n < 1024:
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024
    return f'{n:.1f} GiB'


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    caches: AppCaches = open_caches(settings.root)
    store = Store(settings.root / 'db', settings.load_delay)
    registry = caches.registry
    templates = Jinja2Templates(directory=HERE / 'templates')
    templates.env.filters['size'] = human_size

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        for name in ('pages', 'data', 'rate_limits'):
            registry[name].stats(enable=True)  # hit counts are per process, so every worker turns them on
        yield
        caches.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None)
    app.mount('/static', StaticFiles(directory=HERE / 'static'), name='static')
    app.state.caches = caches

    # -- the slow loaders, memoized in the durable `data` cache ---------------------------------

    def _load_all() -> list[Episode]:
        return store.all()

    def _load_episode(number: int) -> Episode | None:
        return store.get(number)

    load_all = memoized(registry, 'data', expire=FRAGMENT_TTL)(_load_all)
    load_episode = memoized(registry, 'data', expire=FRAGMENT_TTL)(_load_episode)

    def invalidate(number: int) -> None:
        """After an edit: drop every fragment showing the episode, and the loader's results."""
        try:
            pages = registry['pages']
            pages.evict(f'episode:{number}')
            pages.evict('episodes')
            data = registry['data']
            data.delete(data.memoize(expire=FRAGMENT_TTL)(_load_episode).__cache_key__(number))
            data.delete(data.memoize(expire=FRAGMENT_TTL)(_load_all).__cache_key__())
        except Timeout, OSError:
            # evict() raises even on a durable cache. The fragments expire within FRAGMENT_TTL anyway.
            log.warning('could not invalidate episode %s; its cached pages expire within %s s', number, FRAGMENT_TTL)

    # -- sessions: state, in a plain cache, so a failed write raises --------------------------

    def start_session(email: str) -> str:
        sid = secrets.token_urlsafe(32)
        session = {'email': email, 'csrf': secrets.token_urlsafe(16), 'started': time.time()}
        caches.sessions.set(f'session:{sid}', session, expire=IDLE)
        return sid

    def current_user(request: Request) -> dict | None:
        sid = request.cookies.get('sid')
        if not sid:
            return None
        session, expires_at = caches.sessions.get(f'session:{sid}', expire_time=True)
        if session is None or time.time() - session['started'] > ABSOLUTE:
            return None
        if expires_at is not None and expires_at - time.time() < IDLE / 2:  # slide it, but not on every request
            try:
                caches.sessions.touch(f'session:{sid}', expire=IDLE)
            except OSError:  # Timeout included: a missed refresh only shortens the idle window
                log.warning('could not extend a session; it ends sooner')
        return session

    def require_user(request: Request) -> dict:
        user = current_user(request)
        if user is None:
            raise HTTPException(status_code=303, headers={'Location': f'/signin?next={request.url.path}'})
        return user

    def check_csrf(user: dict, token: str) -> None:
        if not secrets.compare_digest(user['csrf'], token):
            raise HTTPException(status_code=400, detail='The form expired. Reload the page and try again.')

    # -- rate limits: a counter per client per window ----------------------------------------

    def allowed(request: Request, scope: str, limit: int, cache: CacheLike) -> tuple[bool, int]:
        """(allowed, remaining). Which cache holds the counters decides what an outage does: on the
        durable `rate_limits` cache a failed write is answered as success, so the limit fails open
        (fine for search); on a state cache it raises, and the request gets a 503 (right for sign-in,
        where a limit that silently stops counting would let a password guesser through)."""
        client = request.client.host if request.client else 'unknown'
        window = int(time.time() // settings.rate_window)
        key = f'rate:{scope}:{client}:{window}'
        cache.add(key, 0, expire=settings.rate_window * 2)
        count = cache.incr(key)
        return count <= limit, max(limit - int(count), 0)

    # -- the hourly claim: exactly one worker queues the sitemap job ----------------------------

    def claim_sitemap() -> None:
        period = int(time.time() // settings.claim_period)
        try:
            won = caches.coordination.add(
                f'claim:sitemap:{period}', {'pid': os.getpid(), 'at': time.time()}, expire=settings.claim_period * 2
            )
        except Timeout:
            log.warning('coordination busy; skipped the sitemap claim this time')
            return
        if won:
            try:
                jobs.enqueue(caches.jobs, 'sitemap')
            except OSError:  # Timeout included
                # Give the claim back, so a later request queues the job this period instead of nobody.
                log.warning('could not queue the sitemap job; releasing the claim')
                try:
                    caches.coordination.delete(f'claim:sitemap:{period}')
                except OSError:
                    pass

    # -- rendering ---------------------------------------------------------------------------

    def render(request: Request, template: str, status: int = 200, **context: object) -> HTMLResponse:
        elapsed = time.perf_counter() - request.state.started
        if 'user' not in context:  # the error pages pass user=None: they must render with no cache at all
            context['user'] = current_user(request)
        context.setdefault('section', '')
        return templates.TemplateResponse(
            request,
            template,
            {'reads': request.state.reads, 'elapsed': elapsed, 'pid': os.getpid(), 'version': __version__, **context},
            status_code=status,
        )

    def fragment(key: str, tag: str, build) -> str:
        """Cache-aside for a rendered piece of HTML, tagged so an edit can evict it."""
        pages = registry['pages']
        html = reads.get(pages, 'pages', key)
        if html is None:
            html = build()
            pages.set(key, html, expire=FRAGMENT_TTL, tag=tag)
        return html

    @app.middleware('http')
    async def track_reads(request: Request, call_next):
        request.state.started = time.perf_counter()
        request.state.reads = reads.start()
        return await call_next(request)

    @app.exception_handler(Timeout)
    def busy(request: Request, exc: Timeout) -> Response:
        # A state cache stayed locked past its timeout: say so instead of pretending it worked.
        return render(
            request, 'message.html', status=503, user=None, title='Busy', message='Please try again in a moment.'
        )

    @app.exception_handler(OSError)
    def unavailable(request: Request, exc: OSError) -> Response:
        # A state cache failed outright (a full disk, a read-only volume): the write didn't happen.
        log.error('request failed on a cache or the disk: %s', exc, exc_info=exc)
        return render(
            request,
            'message.html',
            status=503,
            user=None,
            title='Unavailable',
            message='Something on the server is out of order. Please try again later.',
        )

    @app.exception_handler(StarletteHTTPException)
    def http_error(request: Request, exc: StarletteHTTPException) -> Response:
        if exc.status_code < 400:  # the sign-in redirect
            return Response(status_code=exc.status_code, headers=exc.headers)
        if exc.status_code == 404:
            title, message = 'Not found', 'There is no page at this address.'
        else:
            title, message = 'Something went wrong', str(exc.detail)
        return render(request, 'message.html', status=exc.status_code, user=None, title=title, message=message)

    # -- pages -------------------------------------------------------------------------------

    @app.get('/', response_class=HTMLResponse)
    def episodes(request: Request, q: str = '') -> Response:
        claim_sitemap()
        q = q.strip()
        searches_left = None
        if q:
            ok, searches_left = allowed(request, 'search', settings.search_limit, registry['rate_limits'])
            if not ok:
                return render(
                    request,
                    'message.html',
                    status=429,
                    title='Too many searches',
                    message=f'{settings.search_limit} searches a minute per client. Try again shortly.',
                )
            needle = q.lower()
            found = [e for e in load_all() if needle in f'{e.title} {e.guest} {" ".join(e.tags)}'.lower()]
            body = templates.get_template('_episode_list.html').render(episodes=found)
        else:
            body = fragment(
                'list', 'episodes', lambda: templates.get_template('_episode_list.html').render(episodes=load_all())
            )
        return render(
            request,
            'episodes.html',
            section='episodes',
            body=body,
            q=q,
            searches_left=searches_left,
            search_limit=settings.search_limit,
        )

    @app.get('/episodes/{number}', response_class=HTMLResponse)
    def episode(request: Request, number: int) -> Response:
        def build() -> str:
            found = load_episode(number)
            if found is None:
                raise HTTPException(status_code=404)
            return templates.get_template('_episode_body.html').render(episode=found)

        body = fragment(f'episode:{number}', f'episode:{number}', build)
        return render(request, 'episode.html', section='episodes', body=body, number=number)

    @app.get('/episodes/{number}/edit', response_class=HTMLResponse)
    def edit_form(request: Request, number: int) -> Response:
        user = require_user(request)
        found = store.get(number)  # the editor always reads the source, never the cache
        if found is None:
            raise HTTPException(status_code=404)
        return render(request, 'edit.html', section='episodes', episode=found, csrf=user['csrf'], user=user)

    @app.post('/episodes/{number}/edit')
    def save(
        request: Request,
        number: int,
        title: Annotated[str, Form()],
        description: Annotated[str, Form()],
        minutes: Annotated[int, Form()],
        csrf: Annotated[str, Form()],
        tags: Annotated[str, Form()] = '',  # an episode may have no tags
    ) -> Response:
        check_csrf(require_user(request), csrf)
        if store.get(number) is None:
            raise HTTPException(status_code=404)
        tag_list = tuple(t.strip() for t in tags.replace(',', ' ').split() if t.strip())
        store.update(number, title=title.strip(), description=description.strip(), tags=tag_list, minutes=minutes)
        invalidate(number)
        return RedirectResponse(f'/episodes/{number}', status_code=303)

    @app.post('/episodes/{number}/transcript')
    def request_transcript(request: Request, number: int, csrf: Annotated[str, Form()]) -> Response:
        check_csrf(require_user(request), csrf)
        jobs.enqueue(caches.jobs, 'transcript', episode=number)  # Timeout here is a 503: the job wasn't queued
        return RedirectResponse('/jobs', status_code=303)

    @app.get('/signin', response_class=HTMLResponse)
    def signin_form(request: Request, next: str = '/') -> Response:
        return render(
            request, 'signin.html', section='signin', next=next, attempts_left=None, signin_limit=settings.signin_limit
        )

    @app.post('/signin')
    def signin(
        request: Request, email: Annotated[str, Form()], password: Annotated[str, Form()], next: str = '/'
    ) -> Response:
        # In a state cache, not the durable registry: sign-in fails closed (a 503) if it can't count.
        ok, left = allowed(request, 'signin', settings.signin_limit, caches.coordination)
        if not ok:
            return render(
                request,
                'message.html',
                status=429,
                title='Too many attempts',
                message=f'{settings.signin_limit} sign-in attempts a minute. Try again shortly.',
            )
        if '@' not in email or not password:  # any address works in this demo
            return render(
                request,
                'signin.html',
                section='signin',
                status=400,
                next=next,
                attempts_left=left,
                signin_limit=settings.signin_limit,
                error='Enter an email address and a password.',
            )
        sid = start_session(email.strip())  # a Timeout here is a 503: the session wasn't stored
        target = next if next.startswith('/') and not next.startswith('//') else '/'
        response = RedirectResponse(target, status_code=303)
        response.set_cookie('sid', sid, max_age=ABSOLUTE, httponly=True, samesite='lax', secure=settings.secure_cookies)
        return response

    @app.post('/signout')
    def signout(request: Request) -> Response:
        if sid := request.cookies.get('sid'):
            caches.sessions.delete(f'session:{sid}')
        response = RedirectResponse('/', status_code=303)
        response.delete_cookie('sid')
        return response

    @app.get('/jobs', response_class=HTMLResponse)
    def jobs_page(request: Request) -> Response:
        period = int(time.time() // settings.claim_period)
        claim = caches.coordination.get(f'claim:sitemap:{period}')
        locked = Lock(caches.coordination, 'lock:sitemap').locked()
        return render(
            request,
            'jobs.html',
            section='jobs',
            snap=jobs.snapshot(caches.jobs),
            claim=claim,
            locked=locked,
            now=time.time(),
            csrf=(current_user(request) or {}).get('csrf'),
        )

    @app.get('/admin/caches', response_class=HTMLResponse)
    def admin_caches(request: Request) -> Response:
        local = request.client is not None and request.client.host in {'127.0.0.1', '::1'}
        if not (local or settings.admin_anywhere):
            raise HTTPException(status_code=404)
        rows = []
        for name, stat in registry.stats().items():
            hits, misses = registry[name].stats()
            rows.append(
                {
                    'name': name,
                    'kind': 'durable · registry',
                    'count': stat.count,
                    'volume': stat.volume,
                    'hits': hits,
                    'misses': misses,
                    'error': stat.error,
                }
            )
        for name, cache in caches.state().items():
            rows.append(
                {
                    'name': name,
                    'kind': 'state · eviction off',
                    'count': len(cache),
                    'volume': cache.volume(),
                    'hits': None,
                    'misses': None,
                    'error': None,
                }
            )
        return render(request, 'caches.html', section='caches', rows=rows, root=settings.root)

    @app.get('/health/cache')
    def health() -> Response:
        """A readiness check, never a liveness one: a cache outage is what durable=True absorbs."""
        report: dict = {'caches': {}, 'state': {}}
        for name, stat in registry.stats().items():
            report['caches'][name] = {'up': stat.error is None, 'entries': stat.count, 'bytes': stat.volume}
        ready = True
        for name, cache in caches.state().items():
            try:
                report['state'][name] = {'up': True, 'entries': len(cache)}
            except (Timeout, OSError) as exc:
                report['state'][name] = {'up': False, 'error': type(exc).__name__}
                ready = False  # without its state the app can't sign anyone in or queue work
        report['status'] = 'ok' if ready else 'unavailable'
        return JSONResponse(report, status_code=200 if ready else 503)

    return app
