"""`eviction_policy`, oxide-only (plan 24, release 3): the refused policies, persistence and `reset`
on every kind of cache, a FanoutCache's shards, and a policy changed by another process applying to an
open handle at its next write (architect, H2). diskcache's behavior is in the conformance suite.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from diskcache_oxide import Cache, Caches, FanoutCache, NullCache

MAKERS = [
    pytest.param(lambda p, **kw: Cache(p, **kw), id='Cache'),
    pytest.param(lambda p, **kw: FanoutCache(p, shards=2, **kw), id='FanoutCache'),
    pytest.param(lambda p, **kw: NullCache(p, **kw), id='NullCache'),
]
BIG = b'x' * 100_000


@pytest.mark.parametrize('make', MAKERS)
@pytest.mark.parametrize(
    ('policy', 'error', 'match'),
    [
        ('least-recently-used', ValueError, 'every read a write'),
        ('least-frequently-used', ValueError, 'D-022'),
        ('LEAST-RECENTLY-STORED', ValueError, "'least-recently-stored' or 'none'"),
        (1, TypeError, '(?i)str'),
    ],
)
def test_a_policy_it_cannot_honor_is_refused(
    tmp_path: Path, make: Any, policy: Any, error: type[Exception], match: str
) -> None:
    with pytest.raises(error, match=match):
        make(tmp_path, eviction_policy=policy)


@pytest.mark.parametrize('make', MAKERS)
def test_the_policy_defaults_persists_and_resets(tmp_path: Path, make: Any) -> None:
    cache = make(tmp_path)
    assert cache.eviction_policy == 'least-recently-stored'
    assert cache.reset('eviction_policy') == 'least-recently-stored'
    assert cache.reset('eviction_policy', 'none') == 'none'
    assert cache.eviction_policy == 'none'
    assert cache.reset('eviction_policy') == 'none'
    cache.close()
    if not isinstance(cache, NullCache):
        with make(tmp_path) as again:
            assert again.eviction_policy == 'none', 'None keeps the stored policy'
        with make(tmp_path, eviction_policy='least-recently-stored') as changed:
            assert changed.eviction_policy == 'least-recently-stored'


def test_a_fanout_cache_under_none_evicts_from_no_shard(tmp_path: Path) -> None:
    with FanoutCache(tmp_path, shards=4, size_limit=4 * 2**20, eviction_policy='none') as cache:
        for i in range(120):  # About 12 MB into 4 MiB.
            cache.set(i, BIG)
        assert len(cache) == 120
        assert cache.cull() == 0
        assert cache.reset('eviction_policy', 'least-recently-stored') == 'least-recently-stored'
        assert cache.cull() > 0
        assert len(cache) < 120


RESET_ELSEWHERE = """
import sys
from diskcache_oxide import Cache
with Cache(sys.argv[1]) as c:
    c.reset('eviction_policy', sys.argv[2])
"""


def reset_in_another_process(directory: Path, policy: str) -> None:
    subprocess.run([sys.executable, '-c', RESET_ELSEWHERE, str(directory), policy], check=True, timeout=60)


def test_another_processs_policy_applies_to_an_open_handle_at_its_next_write(tmp_path: Path) -> None:
    with Cache(tmp_path, size_limit=2**20) as cache:
        reset_in_another_process(tmp_path, 'none')
        assert cache.eviction_policy == 'least-recently-stored', 'the attribute is this handle’s last look'
        for i in range(30):
            cache.set(i, BIG)
        assert len(cache) == 30, 'every write read the stored policy'
        assert cache.cull() == 0
        reset_in_another_process(tmp_path, 'least-recently-stored')
        cache.set('one-more', BIG)
        assert len(cache) < 30, 'and back: the next write evicts'


def test_the_caches_registry_passes_the_policy_on(tmp_path: Path) -> None:
    caches = Caches(tmp_path)
    caches.register('pages', eviction_policy='none')
    caches.register('shards', shards=2, eviction_policy='none')
    try:
        assert caches['pages'].eviction_policy == 'none'
        assert caches.fanout('shards').eviction_policy == 'none'
    finally:
        caches.close_all()
