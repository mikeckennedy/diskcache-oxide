"""Headers that show which worker answered, so the sharing is visible (`curl -i`) and testable."""

import os
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

import demo


def worker_headers(get_response: Callable[[HttpRequest], HttpResponse]) -> Callable[[HttpRequest], HttpResponse]:
    # Outside cache_page, so the headers are set on every response, a cached one included, and never stored.
    def middleware(request: HttpRequest) -> HttpResponse:
        response = get_response(request)
        response['X-Worker'] = str(os.getpid())
        response['X-Warmed-In'] = str(demo.warmed_in)
        return response

    return middleware
