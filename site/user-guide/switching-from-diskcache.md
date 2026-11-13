# Switching from python-diskcache

If you're here, the question is a practical one: what breaks? Very little, and on purpose. diskcache-oxide is API compatible with [python-diskcache](https://github.com/grantjenks/python-diskcache) 5.6.3's [Cache](../reference/Cache.md#diskcache_oxide.Cache) and [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache), and it is not data compatible. Keeping diskcache's API makes switching a one-line change. Leaving its on-disk format behind is what made it possible to fix the design underneath, pickle included. So the switch itself is small, and the rest of this page is about what you'll notice afterwards: mostly fixes, and now and then something stricter.

diskcache is also unmaintained. Its last release, 5.6.3, came out in August 2023, the repository has had no commits since 2023, and its open issues and pull requests go unanswered. The bugs this page mentions were found by running diskcache-oxide's test suite against it, and each one has a standalone reproduction in the repository's `upstream/` folder.

For most apps, switching looks like this:

``` python
# from diskcache import Cache, Timeout
from diskcache_oxide import Cache, Timeout

cache = Cache('/var/cache/my-app-oxide')
```


# Start with a fresh cache

diskcache-oxide stores its data in `diskcache-oxide.sqlite`, not diskcache's `cache.db`, and never opens or converts a diskcache database. The two can even share a directory without seeing each other's data. Your cache starts cold after the switch, which for a cache is usually fine. The cleanest switch gives diskcache-oxide a new directory and deletes the old one once every worker has moved. If the two share a directory, diskcache's files are `cache.db*` and its value folders, which nest two levels deep (`3f/a2/….val`); diskcache-oxide's folders are one level deep and hold `.val` files directly, so a two-hex-digit folder that contains folders is diskcache's.


## Upgrading diskcache-oxide

During 0.x, a release that changes the on-disk format bumps the minor version and says so first in its changelog entry. 0.2.3 was one: it added tags and the eviction policy, as format 3. Since 0.3.5, a release that meets a cache in another format doesn't fail: it opens a new, empty cache in the subdirectory `diskcache-oxide-format-N` and logs a warning, so old and new workers each keep a working cache during a rolling deploy. Once the old workers are gone, [remove_old_formats()](../reference/Cache.remove_old_formats.md#diskcache_oxide.Cache.remove_old_formats) removes the old cache. A release that keeps the format keeps your caches warm. The details, including what to do about queues and sessions first, are in [Upgrading across a format change](../recipes/upgrading.md).


# What you might notice

Most of these are fixes. Each one has a conformance test, or an entry in [Divergences](divergences.md) that explains why. If the switch turns up an exception you haven't seen before, [Errors and what to do about them](errors.md) covers each one.

|  | python-diskcache 5.6.3 | diskcache-oxide |
|----|----|----|
| Values | Pickled | msgpack (default) or JSON with exact type tags. Pickle is opt-in with `pickle=True`. |
| Unrepresentable values | Pickled | `TypeError` at [set()](../reference/Cache.set.md#diskcache_oxide.Cache.set), unless `pickle=True` |
| `bool(cache)` | False when empty | Always True; [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) is False |
| [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) | Subclasses `Exception` | Subclasses `TimeoutError` |
| [directory](../reference/Cache.directory.md#diskcache_oxide.Cache.directory) | Optional (makes a temp dir) | Required |
| Clock | Reads `time.time()` | Reads the clock in Rust; pass `clock=` to control it |
| [size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) | Anything (tiny values silently empty the cache) | At least 1 MiB |
| `expire=True` / `timeout=True` | Treated as `1` | `TypeError` |
| `disk=`, `eviction_policy=` | Supported | No `disk=`; `eviction_policy=` is `'least-recently-stored'` or `'none'`, and LRU and LFU raise `ValueError` |
| Queues ([push](../reference/Cache.push.md#diskcache_oxide.Cache.push), [pull](../reference/Cache.pull.md#diskcache_oxide.Cache.pull), [peek](../reference/Cache.peek.md#diskcache_oxide.Cache.peek), [peekitem](../reference/Cache.peekitem.md#diskcache_oxide.Cache.peekitem)) | Any key in the range is an item; [peek](../reference/Cache.peek.md#diskcache_oxide.Cache.peek) writes | Only keys of the queue's exact shape; [peek](../reference/Cache.peek.md#diskcache_oxide.Cache.peek) never writes; [pull](../reference/Cache.pull.md#diskcache_oxide.Cache.pull) decodes first; [Cache](../reference/Cache.md#diskcache_oxide.Cache) only |
| Tags | Compared numerically (`1`, `1.0` and `True` are one tag); `tag` positional | Any key type, compared by exact type; `tag=` keyword-only on [set](../reference/Cache.set.md#diskcache_oxide.Cache.set) and [add](../reference/Cache.add.md#diskcache_oxide.Cache.add) |
| [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) | `tx` is `None` | `tx` is the cache; see [`transact()`](#transact) below |
| [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) | Supported | Supported, with differences; see [`FanoutCache`](#fanoutcache) below |
| Keys | Anything picklable | `str`, `bytes`, `int`, `float`, `bool`, `None` and tuples of those; see [Keys](#keys) below |
| [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) | Supported | Supported, with the same keys; see [Keys](#keys) below |
| `retry=` | Positional; `cache[k]` retries forever | Keyword-only; `cache[k]` raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) |
| [stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats) | Persisted, shared by every process | Per process, in memory (D-049) |
| [check()](../reference/Cache.check.md#diskcache_oxide.Cache.check) | Supported | Supported, with differences (D-050) |
| Locks, semaphores, [throttle](../reference/throttle.md#diskcache_oxide.throttle), [barrier](../reference/barrier.md#diskcache_oxide.barrier), [Averager](../reference/Averager.md#diskcache_oxide.Averager), [memoize_stampede](../reference/memoize_stampede.md#diskcache_oxide.memoize_stampede) | Supported | Supported, with differences; see [Coordination](coordination.md) |
| `Deque`, `Index`, `DjangoCache`, `disk=`, `read=` | Supported | Not available |


# Values: no pickle unless you ask

diskcache pickles every value. That's convenient, but anyone who can write to the cache directory can make your app run arbitrary code the next time it reads, and objects that were never meant to be cached get stored without complaint.

diskcache-oxide stores values as msgpack (or JSON), and tags the types those formats would otherwise change, so tuples, sets, frozensets, datetimes, dates, Decimals, big ints, NaN, nested bytes and non-`str` dict keys all come back exactly as stored. Something that can't be represented raises `TypeError` when you store it, not later when you read it:

``` python
cache.set('job', MyJob())
# TypeError: values of type MyJob can't be stored without pickle (only exact built-in types round-trip)
```

If you really do need to cache arbitrary objects, opt in for that cache:

``` python
objects = Cache('/var/cache/my-app-objects', pickle=True)
```

A cache opened without `pickle=True` never unpickles. If it finds a pickled value written by a `pickle=True` peer, it raises [UnsafeValueError](../reference/UnsafeValueError.md#diskcache_oxide.UnsafeValueError) and leaves the value alone. More in [Serialization](serialization.md).


# Keys

Keys can be `str`, `bytes`, `int` (64-bit), `float`, `bool`, `None`, and tuples of those, nested. diskcache takes anything picklable and pickles what it doesn't store natively; diskcache-oxide never pickles a key, so a `list`, a `dict`, an object, a subclass such as an `IntEnum`, or an int past 64 bits raises `TypeError`. Keys are compared by exact type: `1`, `1.0` and `True` are three keys, where diskcache makes `1` and `1.0` one.

[iterkeys()](../reference/Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys) lists every `str` key first, in sorted order, then the other types (`None`, bools, ints, floats, bytes, tuples), each sorted; `iterkeys(prefix=...)` lists only `str` keys. diskcache sorts numbers, then text, then bytes.

[memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize) works as diskcache's, and `f.__cache_key__(...)` returns the same tuple, with two differences: every argument must be a key type (leave one out with `ignore=`), and `typed=True` stores each argument type's name rather than the class.

`retry=True` is accepted wherever diskcache takes it, as a keyword, and retries only [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout). Dictionary syntax never retries: `cache[k]` raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) after [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout), where diskcache's waits forever.


# [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact)

`with cache.transact():` works as it does in diskcache: the block's calls on the entering thread commit together on exit and roll back on an exception, a nested block joins the outer one, its exit commits nothing, and an exception caught inside the outer block keeps the inner writes. What differs (details in [Transactions](transactions.md)):

- **`tx` is the cache** instead of `None`, and a second [Cache](../reference/Cache.md#diskcache_oxide.Cache) handle on the same directory joins the transaction instead of waiting for it (D-029).
- **A transaction on a second cache whose write lock another thread holds** raises [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy), a [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) subclass, at once, and `retry=True` retries only [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), never [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy). diskcache waits, and its `retry=True` loops on any `OperationalError` (D-030).
- **Another thread may end it.** Its commit or rollback runs within the transaction's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout), and the owner's own exit then raises [TransactionAborted](../reference/TransactionAborted.md#diskcache_oxide.TransactionAborted) (a `RuntimeError`) unless it committed (D-031). The object [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) returns can be entered once.
- **A block that didn't commit raises at its exit** with [TransactionAborted](../reference/TransactionAborted.md#diskcache_oxide.TransactionAborted), a `RuntimeError`, for example after a write inside it failed on a full disk.
- **`durable=True`**, which diskcache doesn't have, still raises when a transaction can't begin and for any failure inside one (D-028).
- **Forking inside your own transaction** poisons the child's copy, whose writes raise `RuntimeError` at once, instead of stalling for the timeout (D-020).
- **It belongs to a thread, not an asyncio task**, as in diskcache: don't `await` inside the block.


# [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache)

[FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) is covered in full in [FanoutCache: sharded caches](fanoutcache.md). The differences from diskcache's, each with an entry in [Divergences](divergences.md):

- **[shards](../reference/FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) is keyword-only** (`FanoutCache(path, shards=4)`). `FanoutCache(path, 4)` is a four-second [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) here. The constructor is `FanoutCache(directory, timeout=None, *, shards=None, ...)`, and [directory](../reference/Cache.directory.md#diskcache_oxide.Cache.directory) is required (D-039).
- **The shard count is recorded**, and a different `shards=` never silently sends keys to other shards: the cache opens with the stored count and logs a warning, or raises [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError) if you pass `raise_on_mismatch=True`. `shards=None` uses the stored count, or creates 8 (D-032).
- **A plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) and a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) refuse each other's directories** with [LayoutMismatchError](../reference/LayoutMismatchError.md#diskcache_oxide.LayoutMismatchError), and nothing is created by the refused open (D-038).
- **Shards are chosen by XXH3, not adler32**, so which shard a key lands in differs from diskcache's, and `shard_of(key)` tells you. Iteration goes shard by shard, and `iterkeys(prefix=, reverse=)` gives the merged sorted order (D-033).
- **A busy shard raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout)** after [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) (60 seconds by default) instead of silently giving False or the default after 10 ms. `durable=True` gives the miss answers, and logs them (D-034).
- **Maintenance shares one [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout)** across the shards, doesn't retry forever, and reports partial progress: a failure raises with `failed_shards` and `removed` (D-035).
- **[size_limit](../reference/Cache.size_limit.md#diskcache_oxide.Cache.size_limit) is a total**, split evenly with at least 1 MiB per shard, and it reads back as the total, not shard 0's share (D-036).
- **`max_connections` is a total** across the shards, so file descriptors are bounded and not one per thread per shard. It must be at least [shards](../reference/FanoutCache.shards.md#diskcache_oxide.FanoutCache.shards) (D-037).
- **[transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) takes a key** and covers one shard: `with cache.transact(key=k):` where diskcache's `with cache.transact():` locks every shard. Calls for keys on other shards inside the block raise `ValueError`, reads included. A ported bare [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) raises `TypeError` (D-040).
- **No queue calls.** [push](../reference/Cache.push.md#diskcache_oxide.Cache.push), [pull](../reference/Cache.pull.md#diskcache_oxide.Cache.pull), [peek](../reference/Cache.peek.md#diskcache_oxide.Cache.peek) and [peekitem](../reference/Cache.peekitem.md#diskcache_oxide.Cache.peekitem) are on [Cache](../reference/Cache.md#diskcache_oxide.Cache) only, since a queue spread over shards would have no order. Use a plain [Cache](../reference/Cache.md#diskcache_oxide.Cache) for a queue.
- **Not there:** `cache()`, `deque()` and `index()`, and `read()` and `read=` (D-039). [memoize](../reference/Cache.memoize.md#diskcache_oxide.Cache.memoize), `retry=`, [stats()](../reference/Cache.stats.md#diskcache_oxide.Cache.stats), [check()](../reference/Cache.check.md#diskcache_oxide.Cache.check), [reset()](../reference/Cache.reset.md#diskcache_oxide.Cache.reset), `reversed()`, pickling, tags with [evict()](../reference/Cache.evict.md#diskcache_oxide.Cache.evict), `eviction_policy=` and settings as attributes all work as on [Cache](../reference/Cache.md#diskcache_oxide.Cache).


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
- File descriptors and memory are bounded by a connection pool, not by how many threads touched the cache. A [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) bounds them for the whole root, not per shard.
- Opening a cache and reading from it never writes to the database.


# What diskcache doesn't have

Once you've switched, a few things are there that diskcache never offered:

- [`durable=True`](getting-started.md#a-cache-that-cant-take-a-request-down) answers a busy lock or a full disk as a miss on single-key calls and logs it once a minute, so a cache outage costs speed instead of failed requests.
- [`NullCache`](null-cache.md) switches caching off without touching call sites, and [`Caches`](caches.md) names an app's caches, opens each on first use and hands out a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) while one can't open.
- [`register_type()`](serialization.md#your-own-types) stores your own classes, dataclasses and library types without pickle.
- A [`FanoutCache` transaction](fanoutcache.md#shard_of-and-transactkey) locks one shard instead of all of them.
- Free-threaded Python (3.14t and 3.15t) is supported and tested on every change.
