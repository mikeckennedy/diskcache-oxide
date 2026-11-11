# New to caching? Start here

If you've never put a cache in front of anything, this page is the ten minutes that make the rest of the docs read easily. If you have, skip to [Getting started](getting-started.md); nothing here is specific to diskcache-oxide except the examples.


# What a cache is

A cache is a copy of an answer, kept somewhere cheaper to reach than wherever the answer came from. The database can tell you the ten most popular episodes, but it takes a few milliseconds and some of its attention to do it, and the answer is the same for everyone who asks in the next five minutes. So you ask once, keep the answer, and hand out the copy.

Something is worth caching when all three of these are true:

- **It's asked for repeatedly.** An answer nobody asks for twice is a copy nobody reads.
- **It costs something to produce.** A database query, a call to another service, rendering a page, a calculation. A cache lookup costs about a microsecond in diskcache-oxide, so anything slower than that is a candidate, and anything that takes milliseconds is a good one.
- **A slightly old answer is acceptable, or you can tell when it stops being right.** This is the one that bites, and the rest of this page is mostly about it.

Here it is in the smallest possible form. The first call does the work; the second gets the copy:


``` python
import tempfile
import time

from diskcache_oxide import Cache

cache = Cache(tempfile.mkdtemp())

def popular_episodes() -> list[str]:
    episodes = cache.get('episodes:popular')          # a hit returns the copy
    if episodes is None:                               # a miss: nothing cached, or it expired
        time.sleep(0.05)                               # stands in for the database query
        episodes = ['Rust for Python developers', 'Free-threaded Python', 'SQLite everywhere']
        cache.set('episodes:popular', episodes, expire=300)
    return episodes

for attempt in ('first call', 'second call'):
    start = time.perf_counter()
    popular_episodes()
    print(f'{attempt}: {(time.perf_counter() - start) * 1000:.2f} ms')
```


    first call: 52.44 ms
    second call: 0.01 ms


That shape, check the cache, do the work on a miss, store the result, is called **cache-aside**, and it's the one you'll use most. The [caching recipe](../recipes/caching-expensive-calls.md) turns it into a decorator.


# Why bother

Three reasons, and you usually get all three at once:

- **Latency.** The page that took 300 ms takes 3, because the slow parts were answers you already had.
- **Load.** The database answers a question once a minute instead of once a request. That's the difference between a database that's fine and one you're upgrading at 2 a.m.
- **Protection.** When the thing behind the cache is slow, flaky or rate-limited (a partner API, a search service), the cache keeps serving the last good answer while it recovers.

The flip side: a cache is a copy, and copies go out of date. Every design decision in caching is a trade between how fresh the answer is and how cheap it is to serve.


# The words

| Word | What it means |
|----|----|
| **Hit / miss** | A lookup found a live entry, or didn't. The **hit rate** is the fraction of lookups that hit; a cache with a low hit rate is costing you writes for nothing. |
| **Key** | The name an entry is stored under. In diskcache-oxide a key is a `str`, like `user:42:profile`. |
| **Expiry (TTL)** | How long an entry stays valid. After it, a lookup misses as if the entry were gone. `expire=300` means five minutes. |
| **Stale** | Still cached, but no longer what the source would say. Expiry bounds how stale an entry can get. |
| **Invalidation** | Removing or replacing an entry because its source changed, instead of waiting for it to expire. |
| **Eviction** | The cache removing entries to stay under its size limit. diskcache-oxide evicts the least recently *stored* entries first. Eviction is why a cache can never be the only copy of something you need. |
| **Cold / warm** | A cold cache is empty (just deployed, just cleared) and every lookup misses until it's warm. The first minutes after a deploy are the slow ones. |
| **Stampede (thundering herd)** | A popular entry expires and every worker recomputes it at the same instant. See [Serving stale values while refreshing](../recipes/stale-while-revalidate.md). |
| **Negative caching** | Caching "there's no such thing" so a missing record doesn't cost a database query every time someone asks. Use a short expiry. |


# The first hard thing: invalidation

> There are only two hard things in Computer Science: cache invalidation and naming things.
>
> Phil Karlton

(The popular extension adds "and off-by-one errors", which is funnier if you don't count.)

Invalidation is hard because the cache can't know when the source changed. It has a copy of the answer; it has no idea the user just renamed themselves. Something has to tell it, and the three ways to do that are the whole of the art:

1.  **Let it expire.** Give every entry an [expire](../reference/Cache.expire.md#diskcache_oxide.Cache.expire), chosen by how stale you can stand: five minutes for a popular list, an hour for a rendered page, a day for a thumbnail. It's the simplest strategy, it needs no knowledge of where the data changes, and it's the one safety net that catches every mistake in the other two. **Always set an expiry, even when you also invalidate explicitly.** An invalidation you forgot is otherwise forever.
2.  **Invalidate at the write.** When your code saves the user's new name, it also deletes `user:42:profile`. The next read misses and rebuilds. This gives exact freshness, at the cost of knowing every key the change affects, which is where naming (below) comes in. When many keys depend on one thing, delete them by prefix, or change a generation number so the old keys are never read again: [Invalidating groups of keys](../recipes/invalidating-groups.md).
3.  **Put the version in the key.** If the key is `episode:512:v7:page`, then publishing revision 8 means readers ask for `episode:512:v8:page`, miss, and rebuild, and the old entry is simply never asked for again. Nothing is deleted; the stale copy expires on its own.

Most apps use all three: an expiry on everything, a delete where the write is obvious, and versioned keys for things that have a natural version. Here's the expiry half, with a fake clock so it runs instantly:


``` python
now = [1_000_000.0]
cache = Cache(tempfile.mkdtemp(), clock=lambda: now[0])

cache.set('user:42:profile', {'name': 'Sam'}, expire=600)
print('right away:   ', cache.get('user:42:profile'))

now[0] += 601                                          # ten minutes pass
print('after expiry: ', cache.get('user:42:profile'))

cache.set('user:42:profile', {'name': 'Sam'}, expire=600)
cache.delete('user:42:profile')                        # the user renamed themselves: invalidate at the write
print('after delete: ', cache.get('user:42:profile'))
```


    right away:    {'name': 'Sam'}
    after expiry:  None
    after delete:  None


One more rule that saves real grief: **never cache a value that depends on who's asking under a key that doesn't say who's asking.** A profile page cached as `page:profile` will be shown to the next user who asks for it. If the answer depends on the user, the locale, the plan, or the A/B group, the key includes them.


# The second hard thing: naming

Keys are names, and the two problems turn out to be the same problem: an invalidation can only find the entries whose names tell it what they depend on. Some conventions that hold up:

- **General to specific, separated by colons:** `user:42:profile`, `episode:512:transcript`. Everything about user 42 then shares the prefix `user:42:`, and a prefix scan (`iterkeys(prefix='user:42:')`) finds all of it in time proportional to how many there are, not the size of the cache.
- **End a prefix with the separator.** `user:4` also matches `user:42`; `user:4:` doesn't.
- **Include everything the value depends on, and nothing it doesn't.** A key with the request's timestamp in it never hits. A key without the locale serves French pages to English readers.
- **Keys are cheap.** A long, descriptive key costs almost nothing; a short, clever one costs you a debugging session later.


# What goes wrong, and the fix

- **Stale data.** Shorter expiry, or an invalidation at the write. If you can't say which writes affect an entry, the expiry is your answer.
- **A stampede after a deploy or an expiry.** Refresh hot entries ahead of time, or serve the stale copy while one worker rebuilds: [Serving stale values while refreshing](../recipes/stale-while-revalidate.md).
- **Cached errors.** Don't cache the result of a failed call as if it were an answer. Cache "not found" briefly if you must; never cache a 500.
- **Treating the cache as storage.** An entry can be evicted or expire at any moment, and a deploy can clear the whole directory. Anything you can't rebuild from somewhere else (a session, a one-time token, a counter you haven't flushed) belongs in a cache of its own, sized so it never evicts, and ideally also somewhere permanent. The [sessions](../recipes/sessions.md) and [counters](../recipes/counters.md) recipes show the pattern.
- **A bug that might be the cache.** Turn caching off without touching the code that uses it, with [`NullCache`](null-cache.md), and see whether the bug survives.


# Where diskcache-oxide fits

There are three usual places to keep a cache, and the choice is about who can see it and what survives:

|  | A dict in the process | diskcache-oxide | A cache server (Redis) |
|----|----|----|----|
| Shared across worker processes | No, one copy per worker | Yes, every process on the machine | Yes, every machine |
| Survives a restart | No | Yes, it's a directory | Depends on configuration |
| Cost of a lookup | Nanoseconds | About a microsecond | A network round trip |
| Something extra to run | No | No | Yes, a server |

The in-process dict is unbeatable for a single process and useless the moment you have four gunicorn workers. The server is the right answer when the cache has to be shared across machines. diskcache-oxide is the middle: a cache that's just a directory, shared by every thread and process on the machine, still there after a restart, and fast enough that you stop thinking about the lookup. [Getting started](getting-started.md) is the next page.
