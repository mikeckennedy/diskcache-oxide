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

Inside the block, a call on this thread for a key on any other shard raises `ValueError`, reads included, through any handle on the root, and so does a [transact](Cache.transact.md#diskcache_oxide.Cache.transact) for a key on another shard (its writes could not be in the transaction). Use [shard_of](FanoutCache.shard_of.md#diskcache_oxide.FanoutCache.shard_of) to check that keys share a shard. Calls over every shard (`len()`, [clear()](Cache.clear.md#diskcache_oxide.Cache.clear) and the rest) are not refused; on the transaction's shard they run inside it.

The check costs one atomic load while no transaction is open anywhere in the process; while one is (any thread's, any cache's), keyed calls also look at each shard that holds one.

As on [Cache](Cache.md#diskcache_oxide.Cache), a transaction belongs to a thread, not an asyncio task, and under `durable=True` a failure to begin, or inside the block, raises.


## Parameters


`key: str`  
Keyword-only. Any key on the shard to hold; it needn't exist.

`retry: bool = ``False`  
As for [Cache.transact()](Cache.transact.md#diskcache_oxide.Cache.transact).


## Raises


`TypeError`  
`key` isn't a `str`, or `retry` isn't a `bool`.


## Examples

``` python
with cache.transact(key='totals:2026-11') as tx:
    n = tx.get('totals:2026-11', 0)
    tx.set('totals:2026-11', n + 1)
```
