"""The WSGI entry point. gunicorn.conf.py loads it once, in the master, before forking the workers."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'demo.settings')

application = get_wsgi_application()

# Touching the cache here, before gunicorn forks (preload_app), is safe: every worker inherits the
# open handle and keeps using it. Not required (opening before or after the fork are both fine); shown
# on purpose, since an app that warms its cache at import does this. It also builds every alias, so a
# misconfigured one (a durable session alias) fails at startup rather than on the first request.
from django.core.cache import cache  # noqa: E402

import demo  # noqa: E402

cache.get('warm-up')
demo.warmed_in = os.getpid()
