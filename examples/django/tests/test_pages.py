"""Each view through Django's test client, in this process."""

import copy
import re

import pytest
from demo import catalog
from diskcache_oxide import DjangoCache
from django.conf import settings
from django.core.cache import caches
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command
from django.test import Client, override_settings


def rendered_at(html: str) -> str:
    match = re.search(r'data-rendered-at="([^"]+)"', html)
    assert match, html
    return match.group(1)


def test_both_aliases_are_diskcache_oxide() -> None:
    assert isinstance(caches['default'], DjangoCache)
    assert isinstance(caches['state'], DjangoCache)
    assert caches['default'].options['durable'] is True
    assert caches['state'].options['durable'] is False
    assert caches['state'].options['eviction_policy'] == 'none'


def test_the_per_view_cache_serves_the_stored_response() -> None:
    client = Client()
    first = client.get('/episode/1')
    second = client.get('/episode/1')
    assert first.status_code == second.status_code == 200
    assert rendered_at(first.text) == rendered_at(second.text)
    assert catalog.loads['episode:1'] == 1
    assert client.get('/episode/2').status_code == 200
    assert catalog.loads['episode:2'] == 1


def test_a_missing_episode_is_404_and_not_cached() -> None:
    client = Client()
    assert client.get('/episode/99').status_code == 404
    assert len(caches['default'].cache) == 0


def test_the_fragment_is_rendered_once() -> None:
    client = Client()
    first = client.get('/').text
    second = client.get('/').text
    assert 'Caching across processes' in first
    assert rendered_at(first) == rendered_at(second)
    assert catalog.loads['episode_list'] == 1


def test_the_session_lives_in_the_state_alias() -> None:
    client = Client()
    assert [client.get('/session').json()['count'] for _ in range(3)] == [1, 2, 3]
    assert Client().get('/session').json()['count'] == 1  # a new visitor
    # Clearing the page cache leaves sessions alone: separate aliases, separate directories.
    caches['default'].clear()
    assert client.get('/session').json()['count'] == 4


def test_get_or_set_computes_once() -> None:
    client = Client()
    assert client.get('/stats').json() == client.get('/stats').json() == {'episodes': 3, 'characters': 75}
    assert catalog.loads['stats'] == 1


def test_visits_count_up() -> None:
    client = Client()
    assert [client.get('/visits').json()['visits'] for _ in range(3)] == [1, 2, 3]


def test_django_checks_pass() -> None:
    call_command('check')


def test_sessions_on_the_durable_alias_would_be_refused() -> None:
    # CACHES too, so Django builds new backend instances: the check runs when an alias's is built.
    with override_settings(SESSION_CACHE_ALIAS='default', CACHES=copy.deepcopy(settings.CACHES)):
        with pytest.raises(ImproperlyConfigured, match='SESSION_CACHE_ALIAS'):
            caches['default']
