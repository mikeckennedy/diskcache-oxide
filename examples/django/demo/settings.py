"""Settings for the Django example: two cache aliases on diskcache-oxide, one for pages, one for state.

`EXAMPLE_ROOT` (default `/var/lib/django-demo`) holds `cache/` (rebuildable: safe to delete on a
deploy) and `state/` (sessions and counters: never delete it with the cache). diskcache-oxide itself
reads no environment variables; this app does, for its own settings.
"""

import os
from pathlib import Path

ROOT = Path(os.environ.get('EXAMPLE_ROOT', '/var/lib/django-demo'))

# A demo: set a real secret and DEBUG off in a real deployment.
SECRET_KEY = os.environ.get('DJANGO_SECRET_KEY', 'django-demo-not-a-secret')
DEBUG = os.environ.get('DJANGO_DEBUG') == '1'
# Name your hosts: the per-view cache keys include the Host header, so with '*' anyone could fill the
# page cache with copies under made-up hosts and evict the real pages.
ALLOWED_HOSTS = os.environ.get('ALLOWED_HOSTS', '127.0.0.1,localhost').split(',')

INSTALLED_APPS = ['demo']  # for its templates; cache sessions need no database and no sessions app
MIDDLEWARE = [
    'demo.middleware.worker_headers',
    'django.contrib.sessions.middleware.SessionMiddleware',
]
ROOT_URLCONF = 'demo.urls'
WSGI_APPLICATION = 'demo.wsgi.application'
DATABASES: dict = {}
USE_TZ = True
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates', 'APP_DIRS': True}]

CACHES = {
    # Pages and fragments: rebuildable, so durable. A write that can't get the lock within 1 s is
    # dropped and a read that fails is a miss: a slower page, never an error page.
    'default': {
        'BACKEND': 'diskcache_oxide.DjangoCache',
        'LOCATION': str(ROOT / 'cache' / 'pages'),
        'TIMEOUT': 600,
        'OPTIONS': {'durable': True, 'size_limit': 512 * 2**20},
    },
    # Sessions and counters: state, not cache, so not under cache/. Not durable (a failed write raises,
    # so a login that didn't stick is an error, not a surprise), and never evicted for size: the
    # directory grows until entries expire. Sessions expire after SESSION_COOKIE_AGE (two weeks); with
    # TIMEOUT None anything else stored here stays until it's deleted. This demo makes a session for
    # every visitor to /, to show it working; a real site shouldn't make one for anonymous visitors.
    'state': {
        'BACKEND': 'diskcache_oxide.DjangoCache',
        'LOCATION': str(ROOT / 'state' / 'site'),
        'TIMEOUT': None,
        'OPTIONS': {'eviction_policy': 'none'},
    },
}

# Sessions in the cache, on the `state` alias. DjangoCache refuses a durable alias here: a dropped
# session write would look like "try again" to Django's session engine.
SESSION_ENGINE = 'django.contrib.sessions.backends.cache'
SESSION_CACHE_ALIAS = 'state'
SESSION_COOKIE_SECURE = os.environ.get('EXAMPLE_SECURE_COOKIES') == '1'

# Keep diskcache-oxide's logger: it's where a durable alias reports what it swallowed.
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'loggers': {
        'diskcache_oxide': {'handlers': ['console'], 'level': 'WARNING'},
        # With DEBUG off, Django sends a 500's traceback only to mail_admins; a failed state write
        # (which raises, by design) should show up in the log too.
        'django.request': {'handlers': ['console'], 'level': 'ERROR'},
    },
}
