"""Django's own cache tests, run against diskcache_oxide.DjangoCache, and the checks of our own that
need every Django line. Run by scripts/django-suite.sh (through django_suite/run.py), never by pytest.

Django's test module is imported as a module, not its classes, so the loader doesn't also collect
the locmem originals from this namespace. Three strict tables say what may not pass, and why; the
runner (`oxide_suite.runner`) fails the run on anything they don't account for.
"""

import asyncio
import functools
import inspect
import os
import shutil
import tempfile
import unittest
from unittest import mock

import cache.tests as django_tests
import sessions_tests.tests as session_tests
from asgiref.sync import async_to_sync
from diskcache_oxide import FanoutCache, djangocache
from diskcache_oxide.djangocache import DjangoCache
from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.base import DEFAULT_TIMEOUT, BaseCache
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.signals import setting_changed

BACKEND = 'diskcache_oxide.DjangoCache'

# Django's culling aliases set MAX_ENTRIES, which DjangoCache refuses (a cache is bounded by bytes), so
# they're left out, as the memcached and Redis test classes leave them out.
NO_CULL = {'cull', 'zero_cull'}

# Tests that skip themselves, by method name. A skip that isn't here, or a test here that runs, fails
# the run.
SKIPS = {
    'test_cull': 'needs the cull alias (MAX_ENTRIES), which is left out',
    'test_zero_cull': 'needs the zero_cull alias (MAX_ENTRIES), which is left out',
    'test_cull_delete_when_store_empty': 'needs the cull alias (MAX_ENTRIES), which is left out',
}

# Tests expected to fail, by class and method, each with its reason. They're marked `expectedFailure`,
# so one that starts passing fails the run.
EXPECTED_FAILURES: dict[str, dict[str, str]] = {}

# Inherited tests that change CACHES or SESSION_CACHE_ALIAS for themselves (to locmem, the dummy backend,
# a failing cache, or an alias that doesn't exist), so they pass without touching ours. Listed so a pass
# there isn't read as coverage. Enforced both ways: a test that changes either setting must be listed,
# and a listed test must change one.
NOT_OURS: dict[str, dict[str, str]] = {
    'OxideCacheSessionTests': {
        'test_non_default_cache': "sets CACHES to the dummy backend and a locmem 'sessions' alias",
    },
    **{
        cls: {
            'test_non_default_cache': "points SESSION_CACHE_ALIAS at an alias that doesn't exist",
            'test_cache_set_failure_non_fatal': "sets CACHES to Django's failing test cache",
            'test_cache_async_set_failure_non_fatal': "sets CACHES to Django's failing test cache",
        }
        for cls in ('OxideCacheDBSessionTests', 'OxideCacheDBSessionWithTimeZoneTests')
    },
}


def open_fds() -> int:
    return len(os.listdir('/dev/fd'))


class Harness:
    """A temporary LOCATION per test, every handle closed before it's deleted, and a descriptor check.

    The descriptor count is taken before the class and after it, and only each test's `release`
    cleanup closes handles, so a test that leaves one open fails the class. A class whose tests never
    opened a handle fails too (when the whole class ran): it tested some other backend.
    """

    @classmethod
    def setUpClass(cls):
        cls.fds_before = open_fds() if os.name != 'nt' else 0
        cls.handles_opened = 0
        cls.tests_released = 0
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if os.name != 'nt':
            after = open_fds()
            if after != cls.fds_before:
                raise AssertionError(f'{cls.__name__} left descriptors open: {cls.fds_before} before, {after} after')
        # Only when every test in the class ran: a filtered run of one test may rightly open nothing.
        whole_class = cls.tests_released == len(unittest.TestLoader().getTestCaseNames(cls))
        if whole_class and not cls.handles_opened:
            raise AssertionError(f'no test in {cls.__name__} opened a DjangoCache')

    # True: each alias gets its own directory (CacheMiddlewareTest's `default` and `other` must be two
    # caches). False: every alias shares one, as BaseCacheTests needs for its prefix and version tests.
    separate_locations = False

    def setUp(self):
        # Before super().setUp(), which in CacheMiddlewareTest and the session tests already takes
        # backend instances from `caches`. As FileBasedCacheTests does: override_settings can't change
        # LOCATION per test, so the settings are changed in place and the signal sent by hand.
        # DjangoCache's receiver closes the previous test's handles.
        self.dirname = tempfile.mkdtemp()
        for alias, params in settings.CACHES.items():
            params['LOCATION'] = os.path.join(self.dirname, alias) if self.separate_locations else self.dirname
        setting_changed.send(self.__class__, setting='CACHES', enter=False)
        # A cleanup, not tearDown: cleanups run after tearDown, last registered first, and Django's own
        # setUp registers some that use the cache (CacheMiddlewareTest's `clear`s), which would reopen
        # it. Registered before super().setUp(), this one runs after all of them.
        self.addCleanup(self.release)
        super().setUp()
        for alias in settings.CACHES:
            self.assertIsInstance(caches[alias], DjangoCache, f'CACHES[{alias!r}] is not ours')
        # Registered last, so it runs first among the cleanups, before Django's and before release.
        self.changed_cache_settings = False
        setting_changed.connect(self.note_cache_settings)
        self.addCleanup(self.check_not_ours)

    def note_cache_settings(self, *, setting, **kwargs):
        if setting in ('CACHES', 'SESSION_CACHE_ALIAS'):
            self.changed_cache_settings = True

    def check_not_ours(self):
        setting_changed.disconnect(self.note_cache_settings)
        listed = self._testMethodName in NOT_OURS.get(type(self).__name__, {})
        if self.changed_cache_settings and not listed:
            self.fail('this test changes CACHES or SESSION_CACHE_ALIAS for itself, and NOT_OURS does not list it')
        if listed and not self.changed_cache_settings:
            self.fail('NOT_OURS lists this test, but it never changed CACHES or SESSION_CACHE_ALIAS')

    def release(self):
        type(self).handles_opened += len(djangocache.handles)
        type(self).tests_released += 1
        djangocache.close_all()
        shutil.rmtree(self.dirname)


def strict(cls):
    # A fresh function per class: expectedFailure marks the function object itself, which every class
    # inheriting the method from BaseCacheTests shares. An async test is wrapped as a sync one that
    # runs it, because Django replaces an async test method with async_to_sync(method) before unittest
    # reads the mark, and that wrapper drops it.
    for name, _reason in EXPECTED_FAILURES.get(cls.__name__, {}).items():
        inherited = getattr(cls, name)
        if inspect.iscoroutinefunction(inherited):

            @functools.wraps(inherited)
            def method(self, inherited=inherited):
                return async_to_sync(inherited)(self)
        else:

            @functools.wraps(inherited)
            def method(self, inherited=inherited):
                return inherited(self)

        setattr(cls, name, unittest.expectedFailure(method))
    return cls


@strict
@override_settings(CACHES=django_tests.caches_setting_for_tests(BACKEND=BACKEND, exclude=NO_CULL))
class OxideCacheTests(Harness, django_tests.BaseCacheTests, TestCase):
    """Django's BaseCacheTests on a plain Cache."""


@strict
@override_settings(
    CACHES=django_tests.caches_setting_for_tests(BACKEND=BACKEND, exclude=NO_CULL, OPTIONS={'shards': 8})
)
class OxideFanoutCacheTests(Harness, django_tests.BaseCacheTests, TestCase):
    """Django's BaseCacheTests on a FanoutCache with 8 shards."""

    def test_every_alias_is_a_fanout_cache(self):
        for alias in settings.CACHES:
            handle = caches[alias].cache
            self.assertIsInstance(handle, FanoutCache, alias)
            self.assertEqual(handle.shards, 8, alias)


@strict
@override_settings(
    # `base` has the lowest precedence, so every alias gets the site prefix except `prefix`, which
    # keeps its own and must still be told apart from `default`.
    CACHES=django_tests.caches_setting_for_tests(base={'BACKEND': BACKEND, 'KEY_PREFIX': 'site'}, exclude=NO_CULL)
)
class OxidePrefixedCacheTests(Harness, django_tests.BaseCacheTests, TestCase):
    """Django's BaseCacheTests with a KEY_PREFIX on every alias, as a site sharing a directory would set."""

    def test_every_alias_but_prefix_carries_the_site_prefix(self):
        # Pins caches_setting_for_tests's precedence: if a Django line changes it, this class would
        # silently become a copy of OxideCacheTests.
        for alias in settings.CACHES:
            self.assertEqual(caches[alias].key_prefix != 'site', alias == 'prefix', alias)


# The framework on top: Django's middleware, utils, i18n and template-response classes, and its cache
# session backends, each hard-wired to locmem in Django and pointed at DjangoCache here. Their other
# class-level settings (CACHE_MIDDLEWARE_KEY_PREFIX and the rest) are inherited.

# The harness sets LOCATION per test; this placeholder fails loudly at first open if anything uses
# an instance built before it does.
UNSET = '/dev/null/set-per-test'


def one():
    return {'default': {'BACKEND': BACKEND, 'LOCATION': UNSET}}


def prefixed():
    return {'default': {'BACKEND': BACKEND, 'LOCATION': UNSET, 'KEY_PREFIX': 'cacheprefix'}}


@strict
@override_settings(CACHES=one())
class OxideCacheUtils(Harness, django_tests.CacheUtils):
    pass


@strict
@override_settings(CACHES=prefixed())
class OxidePrefixedCacheUtils(Harness, django_tests.PrefixedCacheUtils):
    pass


@strict
@override_settings(CACHES=one())
class OxideCacheHEADTest(Harness, django_tests.CacheHEADTest):
    pass


@strict
@override_settings(CACHES=one())
class OxideCacheI18nTest(Harness, django_tests.CacheI18nTest):
    pass


@strict
@override_settings(CACHES=prefixed())
class OxidePrefixedCacheI18nTest(Harness, django_tests.PrefixedCacheI18nTest):
    pass


@strict
@override_settings(
    CACHES={
        'default': {'BACKEND': BACKEND, 'LOCATION': UNSET},
        # As in Django's class: a string TIMEOUT, which BaseCache coerces.
        'other': {'BACKEND': BACKEND, 'LOCATION': UNSET, 'TIMEOUT': '1'},
    }
)
class OxideCacheMiddlewareTest(Harness, django_tests.CacheMiddlewareTest):
    separate_locations = True


@strict
@override_settings(CACHES=one())
class OxideTestWithTemplateResponse(Harness, django_tests.TestWithTemplateResponse):
    pass


@strict
@override_settings(CACHES=one())
class OxideCacheSessionTests(Harness, session_tests.CacheSessionTests):
    pass


@strict
@override_settings(CACHES=one())
class OxideCacheDBSessionTests(Harness, session_tests.CacheDBSessionTests):
    pass


@strict
@override_settings(CACHES=one())
class OxideCacheDBSessionWithTimeZoneTests(Harness, session_tests.CacheDBSessionWithTimeZoneTests):
    pass


# Async methods DjangoCache leaves to BaseCache, built from the ones it overrides.
INHERITED_ASYNC = {'aget_or_set', 'aincr_version', 'adecr_version'}

# Each async method, the sync form it must call, the arguments to pass, and what the sync form gets.
ASYNC_FORMS = [
    ('aadd', 'add', ('k', 'v'), ('k', 'v', DEFAULT_TIMEOUT, None)),
    ('aget', 'get', ('k',), ('k', None, None)),
    ('aset', 'set', ('k', 'v'), ('k', 'v', DEFAULT_TIMEOUT, None)),
    ('atouch', 'touch', ('k',), ('k', DEFAULT_TIMEOUT, None)),
    ('adelete', 'delete', ('k',), ('k', None)),
    ('ahas_key', 'has_key', ('k',), ('k', None)),
    ('aincr', 'incr', ('k',), ('k', 1, None)),
    ('adecr', 'decr', ('k',), ('k', 1, None)),
    ('aget_many', 'get_many', (['k'],), (['k'], None)),
    ('aset_many', 'set_many', ({'k': 'v'},), ({'k': 'v'}, DEFAULT_TIMEOUT, None)),
    ('adelete_many', 'delete_many', (['k'],), (['k'], None)),
    ('aclear', 'clear', (), ()),
]


@override_settings(CACHES={'default': {'BACKEND': BACKEND, 'LOCATION': UNSET}})
class OxideBackendTests(Harness, SimpleTestCase):
    """What BaseCacheTests can't see on every Django line: that each async method calls its sync form.

    From 6.1, BaseCacheTests checks that BaseCache's async methods route to a backend's overridden
    sync ones, but only when the backend leaves the async method alone, and DjangoCache overrides
    every one. On 5.2 and 6.0, BaseCache's `aincr` is an `aget` and then an `aset`: lost updates
    under `asyncio.gather`, and an expiry reset to the alias default.
    """

    def test_every_async_method_is_overridden(self):
        async_names = {
            name
            for name, member in inspect.getmembers(BaseCache)
            if name.startswith('a') and inspect.iscoroutinefunction(member)
        }
        missing = sorted(async_names - INHERITED_ASYNC - set(DjangoCache.__dict__))
        self.assertEqual(missing, [], "BaseCache async methods DjangoCache doesn't override")
        self.assertEqual({form[0] for form in ASYNC_FORMS} | {'aclose'}, async_names - INHERITED_ASYNC)

    async def test_each_async_method_calls_its_sync_form(self):
        cache = caches['default']
        for async_name, sync_name, args, expected in ASYNC_FORMS:
            with self.subTest(async_name), mock.patch.object(DjangoCache, sync_name, return_value='sentinel') as sync:
                self.assertEqual(await getattr(cache, async_name)(*args), 'sentinel')
                sync.assert_called_once_with(*expected)

    async def test_aclose_does_nothing(self):
        cache = caches['default']
        await cache.aset('k', 'v')
        handle = djangocache.handles[cache.directory]
        with mock.patch.object(DjangoCache, 'close') as close:
            self.assertIsNone(await cache.aclose())
            close.assert_not_called()
        self.assertIs(djangocache.handles[cache.directory], handle)

    async def test_aincr_is_atomic(self):
        cache = caches['default']
        await cache.aset('n', 0)
        results = await asyncio.gather(*(cache.aincr('n') for _ in range(100)))
        self.assertEqual(sorted(results), list(range(1, 101)))
        self.assertEqual(await cache.aget('n'), 100)

    async def test_aincr_and_adecr_keep_the_expiry(self):
        cache = caches['default']
        await cache.aset('n', 10, timeout=1000)
        made = cache.make_and_validate_key('n')
        _, before = cache.cache.get(made, expire_time=True)
        self.assertEqual(await cache.aincr('n', 5), 15)
        self.assertEqual(await cache.adecr('n', 3), 12)
        self.assertEqual(cache.cache.get(made, expire_time=True), (12, before))

    async def test_aincr_on_a_missing_key_raises_value_error(self):
        with self.assertRaises(ValueError):
            await caches['default'].aincr('missing')


def check_strict_tables(cases, result) -> list[str]:
    """What the run did that the tables don't account for."""
    problems = []
    if not cases:
        problems.append('no tests were collected')
    if result.testsRun != len(cases):
        problems.append(
            f'{len(cases)} tests collected, {result.testsRun} ran (a class skipped or failed in setUpClass?)'
        )
    collected = {id(test) for test in cases}
    skipped = set()
    for test, reason in result.skipped:
        test = getattr(test, 'test_case', test)  # a skip inside subTest() reports the _SubTest
        if id(test) not in collected:
            problems.append(f'{test} skipped outside a collected test method: {reason}')
        else:
            skipped.add((type(test).__name__, test._testMethodName))
    should_skip = {
        (type(test).__name__, test._testMethodName)
        for test in cases
        if isinstance(test, django_tests.BaseCacheTests) and test._testMethodName in SKIPS
    }
    for cls, name in sorted(skipped - should_skip):
        problems.append(f'{cls}.{name} skipped, and SKIPS does not list it')
    for cls, name in sorted(should_skip - skipped):
        problems.append(f'{cls}.{name} is in SKIPS but ran')
    # A listed method must exist on its class (a renamed Django test), whether or not this run ran it,
    # so a filtered run of one class still passes.
    for table_name, table in (('EXPECTED_FAILURES', EXPECTED_FAILURES), ('NOT_OURS', NOT_OURS)):
        for cls_name, names in table.items():
            cls = globals().get(cls_name)
            for name in names:
                if cls is None or not hasattr(cls, name):
                    problems.append(f'{table_name} lists {cls_name}.{name}, which does not exist')
    return problems


def table_summary(result) -> str:
    listed = sum(
        getattr(getattr(test, 'test_case', test), '_testMethodName', None) in SKIPS for test, _ in result.skipped
    )
    return (
        f'{listed} skipped as SKIPS lists, {len(result.expectedFailures)} expected failures, '
        f'{sum(len(names) for names in NOT_OURS.values())} not ours'
    )
