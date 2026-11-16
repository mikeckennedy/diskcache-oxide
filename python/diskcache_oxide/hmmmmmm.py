"""site_caches: one module that owns the app's caches and hands each out by name."""

from typing import Literal, Optional

from diskcache_oxide import Caches, TransactionalCacheLike

CacheName = Literal['pages', 'markdown', 'etags']
caches: Optional[Caches[CacheName]] = None


def setup(base_folder: str) -> None:
    """Register the app's caches. Call once at startup; later calls do nothing."""
    global caches
    if caches is not None:
        return

    registry: Caches[CacheName] = Caches(base_folder, durable=True, timeout=1.0)
    registry.register('pages')
    registry.register('markdown')
    registry.register('etags')
    caches = registry


def close() -> None:
    """Close every cache, at shutdown. `setup()` can be called again afterwards."""
    global caches
    if caches is not None:
        caches.close_all()
        caches = None


def cache(name: CacheName) -> TransactionalCacheLike:
    if caches is None:
        raise RuntimeError('call site_caches.setup() first')
    return caches[name]


def pages_cache() -> TransactionalCacheLike:
    return cache('pages')


def markdown_cache() -> TransactionalCacheLike:
    return cache('markdown')


def etag_cache() -> TransactionalCacheLike:
    return cache('etags')


# ################ Usage:
# import site_caches
#
# ################ At start up
# site_caches.setup('./caches')
#
# ################ At usage, pseudo code of course:
# def render_page(url: str) -> Response:
#     etag: Optional[str] = site_caches.etag_cache().get(url)
#     page: Optional[str] = site_caches.pages_cache().get(url)
#
#     if page is not None:
#         return Response(body=page, etag=etag)
#
#     html = render_page_contents(url)
#     etag = etag_from_body(html)
#
#     site_caches.etag_cache().set(url, etag)
#     site_caches.pages_cache().set(url, html)
#
#     return Response(body=html, etag=etag)
