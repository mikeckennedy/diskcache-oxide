"""Settings for Django's cache tests run against diskcache_oxide.DjangoCache (django_suite/run.py).

The test classes override `CACHES` themselves; the locmem default here is only what Django starts
with. The rest is the part of Django's `tests/runtests.py` that the cache tests need.
"""

SECRET_KEY = 'diskcache-oxide-django-suite'
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3'}}
INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.auth',
    'django.contrib.sessions',
    'cache',  # Django's own test app (tests/cache), for its Poll model
    'sessions_tests',  # Django's session tests' app, for its custom session model
    'oxide_suite',
]
USE_TZ = False
DEFAULT_AUTO_FIELD = 'django.db.models.AutoField'
PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']
CACHES = {'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}}
# For the middleware, i18n and template-response classes, as in runtests.py.
LANGUAGE_CODE = 'en'
ROOT_URLCONF = 'urls'
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {'context_processors': ['django.template.context_processors.request']},
    }
]
TEST_RUNNER = 'oxide_suite.runner.StrictRunner'
