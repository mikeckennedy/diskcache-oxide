"""Django set up once for the in-process tests, on a temporary EXAMPLE_ROOT."""

import atexit
import os
import shutil
import tempfile
from collections.abc import Iterator

import pytest

ROOT = tempfile.mkdtemp(prefix='django-demo-')
os.environ['EXAMPLE_ROOT'] = ROOT
os.environ['EXAMPLE_LOAD_DELAY'] = '0'
os.environ['ALLOWED_HOSTS'] = 'testserver'
os.environ['DJANGO_SETTINGS_MODULE'] = 'demo.settings'

import django  # noqa: E402

django.setup()


def remove_root() -> None:
    from diskcache_oxide import djangocache

    djangocache.close_all()
    shutil.rmtree(ROOT, ignore_errors=True)


atexit.register(remove_root)

from demo import catalog  # noqa: E402
from django.core.cache import caches  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_caches() -> Iterator[None]:
    for alias in ('default', 'state'):
        caches[alias].clear()
    catalog.loads.clear()
    yield
