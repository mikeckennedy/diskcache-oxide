# Switching from python-diskcache

diskcache-oxide is **API compatible** with [python-diskcache](https://github.com/grantjenks/python-diskcache) 5.6.3's [Cache](../reference/Cache.md#diskcache_oxide.Cache), and deliberately **not data compatible**. Keeping diskcache's API makes switching a one-line change. Not keeping its on-disk format is what allowed fixing its design instead of inheriting it.

For most apps, switching looks like this:

``` python
# from diskcache import Cache, Timeout
from diskcache_oxide import Cache, Timeout

cache = Cache('/var/cache/my-app-oxide')
```


# Start with a fresh cache

diskcache-oxide stores its data in `diskcache-oxide.sqlite`, not diskcache's `cache.db`, and never opens or converts a diskcache database. The two can even share a directory without seeing each other's data. Your cache starts cold after the switch, which for a cache is usually fine. Once you've switched, delete the old `cache.db*` files and value files to get the disk space back.


# What you might notice

Most of these are fixes. Each one has a conformance test, or an entry in [Divergences](divergences.md) that explains why.

|  | python-diskcache 5.6.3 | diskcache-oxide |
|----|----|----|
| Values | Pickled | msgpack (default) or JSON with exact type tags. Pickle is opt-in with `pickle=True`. |
| Unrepresentable values | Pickled | `TypeError` at [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set), unless `pickle=True` |
| Keys | Anything picklable | `str` only for now |
| `bool(cache)` | False when empty | Always True; [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) is False |
| [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) | Subclasses `Exception` | Subclasses `TimeoutError` |
| [directory](../reference/Cache.directory.md#diskcache_oxide.Cache.directory) | Optional (makes a temp dir) | Required |
| Clock | Reads `time.time()` | Reads the clock in Rust; pass `clock=` to control it |
| [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) | Anything (tiny values silently empty the cache) | At least 1 MiB |
| `expire=True` / `timeout=True` | Treated as `1` | `TypeError` |
| `disk=`, `eviction_policy=` | Supported | Not supported (eviction policies are coming) |
| `transact()`, tags, `memoize`, `FanoutCache` | Supported | Coming in later phases |


# Values: no pickle unless you ask

diskcache pickles every value. That's convenient, but anyone who can write to the cache directory can make your app run arbitrary code the next time it reads, and objects that were never meant to be cached get stored without complaint.

diskcache-oxide stores values as msgpack (or JSON), and tags the types those formats would otherwise change, so tuples, sets, datetimes, big ints, NaN, nested bytes and non-`str` dict keys all come back exactly as stored. Something that can't be represented raises `TypeError` when you store it, not later when you read it:

``` python
cache.set('job', MyJob())
# TypeError: values of type MyJob can't be stored without pickle (only exact built-in types round-trip)
```

If you really do need to cache arbitrary objects, opt in for that cache:

``` python
objects = Cache('/var/cache/my-app-objects', pickle=True)
```

A cache opened without `pickle=True` never unpickles. If it finds a pickled value written by a `pickle=True` peer, it raises [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) and leaves the value alone. More in [Serialization](serialization.md).


# String keys

Only `str` keys are accepted today; anything else raises `TypeError`. Keys are already stored with a type tag, so other key types can be added in Phase 6a without changing the file format. In the meantime, build string keys yourself (`f'user:{user_id}'`).


# Truthiness

In diskcache, an empty cache is falsy, so `if cache:` quietly skips caching right after every deploy, when the cache is empty. In diskcache-oxide, `bool(Cache)` is always True and `bool(NullCache)` is always False, so `if cache:` means "is caching on?". Both have an [enabled](../reference/Cache.enabled.md#diskcache_oxide.Cache.enabled) property that says the same thing.


# Tests that patch `time.time`

diskcache reads `time.time()` in Python, so tests could advance its clock with `unittest.mock.patch` or a freezegun-style fixture. diskcache-oxide reads the clock natively, which keeps a Python call off every operation. To make it follow a patched clock, pass `clock=`:

``` python
cache = Cache(tmp_path, clock=lambda: time.time())

with patch('time.time', return_value=1000.0):
    cache.set('k', 'v', expire=10)
with patch('time.time', return_value=1015.0):
    assert cache.get('k') is None
```


# Things that just get better

You don't have to change anything for these:

- Reads take about half the time, and threaded throughput no longer collapses as threads are added. See [Performance](performance.md).
- A child process forked while another thread was writing can always write. In diskcache it can stall for the full 60-second timeout.
- [clear()](../reference/Cache.clear.md#diskcache_oxide.Cache.clear) keeps writes that land while it runs, [expire()](../reference/Cache.expire.md#diskcache_oxide.Cache.expire) removes every expired entry, and a read during an overwrite never misses.
- File descriptors and memory are bounded by a connection pool, not by how many threads touched the cache.
- Opening a cache and reading from it never writes to the database.
