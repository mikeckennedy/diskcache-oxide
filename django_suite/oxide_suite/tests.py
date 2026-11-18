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

# Inherited tests that set CACHES to another backend for themselves (locmem, dummy, a failing cache),
# so they pass without touching ours. Listed so a pass there isn't read as coverage.
NOT_OURS: dict[str, dict[str, str]] = {}


def open_fds() -> int:
    return len(os.listdir('/dev/fd'))


class Harness:
    """A temporary LOCATION per test, every handle closed before it's deleted, and a descriptor check.

    The descriptor count is taken before the class and after it, and only `tearDown` closes handles,
    so a test that leaves one open fails the class.
    """

    @classmethod
    def setUpClass(cls):
        cls.fds_before = open_fds() if os.name != 'nt' else 0
        super().setUpClass()

    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        if os.name != 'nt':
            after = open_fds()
            if after != cls.fds_before:
                raise AssertionError(f'{cls.__name__} left descriptors open: {cls.fds_before} before, {after} after')

    def setUp(self):
        super().setUp()
        self.dirname = tempfile.mkdtemp()
        # As FileBasedCacheTests does: override_settings can't change LOCATION per test, so the
        # settings are changed in place and the signal sent by hand. DjangoCache's receiver closes
        # the previous test's handles.
        for params in settings.CACHES.values():
            params['LOCATION'] = self.dirname
        setting_changed.send(self.__class__, setting='CACHES', enter=False)

    def tearDown(self):
        super().tearDown()  # BaseCacheTests clears the cache
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


@override_settings(CACHES={'default': {'BACKEND': BACKEND, 'LOCATION': 'set per test'}})
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
    ran = {(type(test).__name__, test._testMethodName) for test in cases}
    for table_name, table in (('EXPECTED_FAILURES', EXPECTED_FAILURES), ('NOT_OURS', NOT_OURS)):
        for cls, names in table.items():
            for name in names:
                if (cls, name) not in ran:
                    problems.append(f'{table_name} lists {cls}.{name}, which did not run')
    return problems


def table_summary(result) -> str:
    listed = sum(
        getattr(getattr(test, 'test_case', test), '_testMethodName', None) in SKIPS for test, _ in result.skipped
    )
    return (
        f'{listed} skipped as SKIPS lists, {len(result.expectedFailures)} expected failures, '
        f'{sum(len(names) for names in NOT_OURS.values())} not ours'
    )
