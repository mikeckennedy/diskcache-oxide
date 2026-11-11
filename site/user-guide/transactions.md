# Transactions

Every write is already its own transaction, atomic across every thread and process using the directory. Sometimes one write at a time isn't enough. A request bumps a total, bumps a per-path counter and records when it happened, and a reader that sees two of those three has seen something that was never true. [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) groups several calls into one: they all commit together when the block exits, or none of them do.

``` python
with cache.transact() as tx:
    tx.incr('requests')
    tx.incr(f'requests:{path}')
    tx.set('last_request', now)
```

That's also faster. Each commit costs a disk sync, so grouping writes pays for one instead of several: three counter bumps measured 79 µs one by one and 48 µs in one transaction.


# What the block does

- **Commit on exit, roll back on an exception.** If the block raises, none of its writes are kept, and the exception carries on as usual.
- **`tx` is the cache itself.** On the thread that entered the block, every call on that cache directory inside it runs in the transaction, whether you call it on `tx`, on `cache`, or on another [Cache](../reference/Cache.md#diskcache_oxide.Cache) handle for the same directory. diskcache's [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) yields `None`, so code written for it keeps working.
- **The block reads its own writes.** Other threads and processes see nothing until it commits, and their writes to this directory wait for it, up to their own [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout). Keep blocks short.
- **Nesting joins the outer block.** A [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) inside another one on the same directory, on the same thread, joins the outer transaction instead of starting its own. As in diskcache, the inner block's exit commits nothing, and an exception you catch inside the outer block doesn't undo the inner block's writes. Only the outer block's exit commits.

``` python
with cache.transact():
    cache.set('a', 1)
    try:
        with cache.transact():
            cache.set('b', 2)
            raise ValueError
    except ValueError:
        pass
# Both 'a' and 'b' are committed: the inner block had no savepoint of its own.
```

The object [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) returns is used once: entering it a second time, or while another thread is entering it, raises `RuntimeError`.


# Beginning one: [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) and `retry=`

Beginning a transaction takes the directory's write lock, and waits for it up to the cache's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout), then raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), as a write does. With `retry=True` it keeps trying instead. Each attempt waits up to [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout) with the interpreter released, and `Ctrl-C` is checked between attempts, so a `KeyboardInterrupt` gets through within one [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout).

``` python
with cache.transact(retry=True) as tx:
    ...
```


## Two caches at once: [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy)

Inside one cache's transaction, beginning a transaction on a second cache takes that cache's write lock if it's free. If another thread holds it, [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) raises [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy) at once instead of waiting: that thread could be waiting for the lock you hold, and then neither would ever finish. [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy) is a subclass of [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout), so existing handlers still catch it, and `retry=True` never retries it, since trying again while you still hold the first lock can't help.

To use two caches in one block, always nest them in the same order everywhere in your app, and if [TransactionBusy](../reference/TransactionBusy.md#diskcache_oxide.TransactionBusy) reaches you, leave the outer block and try the whole thing again.


# When a block doesn't commit

A `with` block never exits cleanly when its writes are gone. Its exit raises `TransactionAborted`, a `RuntimeError`, when the transaction didn't commit:

- an operation inside it failed in a way that left the transaction unusable (a full disk, an I/O error). Later writes in the block fail at once, and nothing in it commits;
- it was ended from another thread, which rolled it back (below);
- it was ended from inside one of its own operations, such as a pickle hook that calls the block's [__exit__](../reference/Cache.__exit__.md#diskcache_oxide.Cache.__exit__). That raises `RuntimeError` where it's called, and the transaction rolls back when that operation returns.

A commit that can't finish raises too: a busy or failed `COMMIT` raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) or an `OSError`, and nothing is kept.


## Ending it from another thread

diskcache says a transaction "may not be shared between threads". In diskcache-oxide, any thread may call the transaction's [__exit__](../reference/Cache.__exit__.md#diskcache_oxide.Cache.__exit__), to commit (no exception) or roll back the owner's transaction. The rules:

- While the owner is inside one of its own operations (decoding a value, say), a rollback is left to that operation, and happens when it returns. A commit waits for it, up to the cache's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout). A commit that can't run by then raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) and rolls back.
- The owner's own exit afterwards raises `TransactionAborted` unless the transaction committed.
- An owner's exit that comes while the other thread is still committing waits for the outcome, up to the cache's [timeout](../reference/Cache.timeout.md#diskcache_oxide.Cache.timeout), and reports it. Past that, it raises [Timeout](../reference/Timeout.md#diskcache_oxide.Timeout) ("whether it committed is unknown"). An exit with an exception passing doesn't wait.
- Writes the owner makes after another thread ended the transaction run on their own, outside it.


# `durable=True`

On a [`durable=True`](getting-started.md#a-cache-that-cant-take-a-request-down) cache, beginning a transaction still raises, and so does any failure inside the block: dropping one write and committing the rest would break the transaction's atomicity. Only calls outside a transaction are answered as misses.


# A transaction belongs to a thread, not a task

The calls that join a transaction are the ones made on the thread that entered the block. In asyncio, every task on a loop runs on that same thread. So if you `await` inside the block, another coroutine that runs meanwhile and writes to the same cache joins your transaction, and rolls back with it if your block raises. Don't `await` inside a [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) block. An `async with cache.transact_async()` form, where waiting for the write lock wouldn't block the event loop, is part of the planned `AsyncCache` experiment (see the [roadmap](roadmap.md)).


# Transactions and forks

A fork waits for every transaction other threads hold, with no timeout, so the child never inherits half of one (D-012). A long transaction holds up every fork in the process while it's open, and calls behind that fork time out (see [How long a call waits for a fork](concurrency-and-forks.md#how-long-a-call-waits-for-a-fork)). Forking from inside your own transaction can't be waited for: the child's copy is poisoned and raises `RuntimeError` (see [Forking from inside your own transaction](concurrency-and-forks.md#forking-from-inside-your-own-transaction)).


# Typing: [TransactionalCacheLike](../reference/TransactionalCacheLike.md#diskcache_oxide.TransactionalCacheLike)

[Cache](../reference/Cache.md#diskcache_oxide.Cache) and [`NullCache`](null-cache.md) both have [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact). Annotate with [TransactionalCacheLike](../reference/TransactionalCacheLike.md#diskcache_oxide.TransactionalCacheLike) to accept either and call it:

``` python
from diskcache_oxide import TransactionalCacheLike

def count(cache: TransactionalCacheLike, path: str) -> None:
    with cache.transact() as tx:
        tx.incr('requests')
        tx.incr(f'requests:{path}')
```

On a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache), [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) is a no-op that yields the [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache). A [`FanoutCache`](fanoutcache.md) is not a [TransactionalCacheLike](../reference/TransactionalCacheLike.md#diskcache_oxide.TransactionalCacheLike): its `transact(*, key)` covers only the shard that `key` lives in, and refuses calls for keys on other shards inside the block. Annotate with [ShardedCacheLike](../reference/ShardedCacheLike.md#diskcache_oxide.ShardedCacheLike) to accept a [FanoutCache](../reference/FanoutCache.md#diskcache_oxide.FanoutCache) or a [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache). [CacheLike](../reference/CacheLike.md#diskcache_oxide.CacheLike) itself has no [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact), so a cache whose transactions need more than `retry=` (a sharded cache's `transact(key=...)`) can still be a [CacheLike](../reference/CacheLike.md#diskcache_oxide.CacheLike). A [`Caches`](caches.md) lookup is a [TransactionalCacheLike](../reference/TransactionalCacheLike.md#diskcache_oxide.TransactionalCacheLike), so `caches['general'].transact()` type-checks, and a disabled cache's [transact()](../reference/Cache.transact.md#diskcache_oxide.Cache.transact) is the [NullCache](../reference/NullCache.md#diskcache_oxide.NullCache) one.
