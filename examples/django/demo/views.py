"""Each view shows one way Django uses the cache."""

import os
import time

from django.contrib.sessions.backends.base import SessionBase
from django.core.cache import cache, caches
from django.http import Http404, HttpRequest, HttpResponse, JsonResponse
from django.shortcuts import render
from django.views.decorators.cache import cache_page

from demo import catalog


def session_of(request: HttpRequest) -> SessionBase:
    """The request's session. SessionMiddleware adds it, which a type checker can't see."""
    return request.session  # ty: ignore[unresolved-attribute]


def home(request: HttpRequest) -> HttpResponse:
    """A template fragment (`{% cache %}` in home.html) and a session counter."""
    session = session_of(request)
    session['views'] = session.get('views', 0) + 1
    return render(
        request,
        'demo/home.html',
        {
            # A callable, so the template only calls it on a fragment miss.
            'episodes': catalog.episode_list,
            'rendered_at': time.time(),
            'views': session['views'],
        },
    )


# Don't read the session in a cache_page view: SessionMiddleware adds `Vary: Cookie` after the
# decorator has stored the response, so one visitor's page would be served to everyone. A view that
# must, needs @vary_on_cookie as well.
@cache_page(60)
def episode(request: HttpRequest, number: int) -> HttpResponse:
    """The per-view cache: the whole response is stored (pickled) and served from the cache for 60 s."""
    if number not in catalog.EPISODES:
        raise Http404('no such episode')
    return render(request, 'demo/episode.html', {'episode': catalog.load_episode(number), 'rendered_at': time.time()})


def stats(request: HttpRequest) -> JsonResponse:
    """The low-level API: computed once, then read from the cache for five minutes."""
    return JsonResponse(cache.get_or_set('stats', catalog.compute_stats, 300))


def visits(request: HttpRequest) -> JsonResponse:
    """A counter every worker shares: `incr` is atomic across processes."""
    state = caches['state']
    state.add('visits', 0)  # only the first one wins
    return JsonResponse({'visits': state.incr('visits'), 'pid': os.getpid()})


def session_count(request: HttpRequest) -> JsonResponse:
    """The session, wherever the request lands: every worker reads the same cache."""
    session = session_of(request)
    session['count'] = session.get('count', 0) + 1
    return JsonResponse({'count': session['count'], 'pid': os.getpid()})


def health(request: HttpRequest) -> HttpResponse:
    return HttpResponse(str(os.getpid()), content_type='text/plain')
