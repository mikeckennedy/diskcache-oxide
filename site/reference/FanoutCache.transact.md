## FanoutCache.transact()


A write transaction on the shard `key` lives in, for `with cache.transact(key='total') as tx:`.


Usage

``` python
FanoutCache.transact(
    *,
    key,
    retry=False,
)
```


It is [Cache.transact()](Cache.transact.md#diskcache_oxide.Cache.transact) on that one shard: `tx` is this FanoutCache, calls on this thread inside the block for keys on that shard run in the transaction (through any handle on the root), it commits when the block exits and rolls back if the block raises, and a nested [transact](Cache.transact.md#diskcache_oxide.Cache.transact) for a key on the same shard joins it. Other threads' and processes' writes to that shard wait for it; the other shards are not locked.

Inside the block, a call on this thread for a key on any other shard raises `ValueError`, reads included, through any handle on the root, and so does a [transact](Cache.transact.md#diskcache_oxide.Cache.transact) for a key on another shard (its writes could not be in the transaction). Use [shard_of](FanoutCache.shard_of.md#diskcache_oxide.FanoutCache.shard_of) to check that keys share a shard. Calls over every shard raise `ValueError` on this thread inside the block too: `len()`, [volume()](Cache.volume.md#diskcache_oxide.Cache.volume), `iter()`, [iterkeys()](Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys) (and `next()` on an iterator made before the block), [clear()](Cache.clear.md#diskcache_oxide.Cache.clear), [expire()](Cache.expire.md#diskcache_oxide.Cache.expire), [cull()](Cache.cull.md#diskcache_oxide.Cache.cull) and [checkpoint()](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint). They would run inside the transaction on its shard and outside it on the others.

The check costs one atomic load while no transaction is open anywhere in the process; while one is (any thread's, any cache's), keyed calls, calls over every shard and iterator `next()`s also look at each shard that holds one.

As on [Cache](Cache.md#diskcache_oxide.Cache), a transaction belongs to a thread, not an asyncio task, a block that didn't commit raises [TransactionAborted](TransactionAborted.md#diskcache_oxide.TransactionAborted) at its exit, and under `durable=True` a failure to begin, or inside the block, raises.


## Parameters


`key: Key`  
Keyword-only. Any key on the shard to hold; it needn't exist.

`retry: bool = ``False`  
As for [Cache.transact()](Cache.transact.md#diskcache_oxide.Cache.transact).

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Raises


`TypeError`  
`key` isn't a `str`, or `retry` isn't a `bool`.


## Examples

``` python
with cache.transact(key='totals:2026-11') as tx:
    n = tx.get('totals:2026-11', 0)
    tx.set('totals:2026-11', n + 1)
```
