# Testing code that uses a cache

Code that uses a cache has a talent for passing its tests for the wrong reason: the second assertion was green because the first one left the answer behind, and the test that "covers" expiry sleeps for two seconds and hopes. Such code needs tests of two kinds: tests where caching shouldn't get in the way, and tests of the caching behavior itself (is the value cached, does it expire, is it invalidated). diskcache-oxide gives you a tool for each, and neither involves sleeping.


# A real cache per test

A [Cache](../reference/Cache.md#diskcache_oxide.Cache) in pytest's `tmp_path` is fast and isolated: each test gets its own empty cache, and it's thrown away afterwards.

``` python
import pytest

from diskcache_oxide import Cache

@pytest.fixture
def cache(tmp_path):
    with Cache(tmp_path / 'cache') as cache:
        yield cache
```


# No caching at all: [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache)

For tests that exercise your app rather than its caching, a cached value from an earlier test can hide a bug. Swap in a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache): every read misses and every write succeeds, so every call goes to the real code path. It has the same methods and signatures as [Cache](../reference/Cache.md#diskcache_oxide.Cache), so nothing else changes.

``` python
from diskcache_oxide import NullCache

@pytest.fixture
def app_cache(monkeypatch):
    null = NullCache()
    monkeypatch.setattr('my_app.services.cache', null)
    return null
```

One thing a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) can't catch: it never encodes values, so a [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set) of a value a real cache refuses (a dataclass you haven't registered, an ORM row, a `dict` subclass) succeeds on a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) and raises `TypeError` in production. Keep at least one test per type you store that runs against a real [Cache](../reference/Cache.md#diskcache_oxide.Cache) on `tmp_path`, using the [cache](../reference/DjangoCache.cache.md#diskcache_oxide.DjangoCache.cache) fixture above. It's also how you find out a type needs [`register_type`](custom-types.md).


# Testing expiry without sleeping

diskcache-oxide reads the clock natively in Rust, so patching `time.time` doesn't affect it on its own. Pass `clock=` instead. A small fake clock lets a test jump forward in time:


``` python
import tempfile

from diskcache_oxide import Cache

class FakeClock:
    def __init__(self, now: float = 1_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

clock = FakeClock()
cache = Cache(tempfile.mkdtemp(), clock=clock)

cache.set('session:abc', {'user': 42}, expire=30)
clock.advance(29)
print('after 29 s:', cache.get('session:abc'))
clock.advance(2)
print('after 31 s:', cache.get('session:abc'))
```


    after 29 s: {'user': 42}
    after 31 s: None


As a fixture:

``` python
@pytest.fixture
def clock():
    return FakeClock()

@pytest.fixture
def cache(tmp_path, clock):
    with Cache(tmp_path / 'cache', clock=clock) as cache:
        yield cache

def test_sessions_expire(cache, clock):
    cache.set('session:abc', {'user': 42}, expire=30)
    clock.advance(31)
    assert cache.get('session:abc') is None
```

If your tests already patch `time.time` (with `unittest.mock`, freezegun or time-machine) and you want the cache to follow along, pass `clock=lambda: time.time()`. The lambda looks up `time.time` on every call, so it sees the patched version.


# Background maintenance in tests

Background maintenance runs on the wall clock, so it never runs for a cache with its own `clock`: leave `maintenance_interval` out, or pass 0, since any other value with `clock=` raises `ValueError`. One `clock=` handle turns maintenance off for its directory in the whole process, other handles on it included, until every handle on that directory has closed.

A cache's first turn comes at least one interval after it opens, so a test shorter than 5 minutes never sees maintenance at the default. A long run, or a suite that sets a short `maintenance_interval`, can see it remove expired entries between two lines of a test, which changes what an [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) or [cull()](../reference/Cache.cull.md#diskcache_oxide.Cache.cull) returns and what [volume()](../reference/Cache.volume.md#diskcache_oxide.Cache.volume) says. Pass `maintenance_interval=0` to every handle the test opens on those directories: one that asks for an interval keeps maintenance on for all of them.

A hand-written fake that stands in for a cache, typed as [`CacheLike`](../user-guide/null-cache.md#cachelike), needs [maintenance_stats()](../reference/Cache.maintenance_stats.md#diskcache_oxide.Cache.maintenance_stats) and [remove_old_formats()](../reference/Cache.remove_old_formats.md#diskcache_oxide.Cache.remove_old_formats) too. [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) already has both.


# Testing code that runs in several processes

Code that relies on the cache being shared, like a job claim or a rate limit, can be tested for real: start processes with `multiprocessing` and point them at the same `tmp_path` directory. diskcache-oxide is safe with every start method, including fork.

``` python
import multiprocessing

def claim(directory: str) -> bool:
    with Cache(directory) as cache:
        return cache.add('job:nightly', 'claimed', expire=60)

def test_only_one_worker_claims_the_job(tmp_path):
    with multiprocessing.Pool(8) as pool:
        results = pool.map(claim, [str(tmp_path)] * 8)
    assert results.count(True) == 1
```

A [Cache](../reference/Cache.md#diskcache_oxide.Cache) also pickles: the copy a `multiprocessing` worker receives reopens the same directory with the same options, so you can pass the cache itself instead of its path.
