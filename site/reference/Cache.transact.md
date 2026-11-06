## Cache.transact()


A write transaction, for `with cache.transact() as tx:`.


Usage

``` python
Cache.transact(retry=False)
```


`tx` is the cache itself. On the thread that entered the block, every call on this cache directory inside it, through any handle, runs in the transaction: it commits when the block exits and rolls back if the block raises. Other threads' and processes' writes wait for it. Entered on a thread that already holds a transaction on this directory, it joins that one: the inner block's exit doesn't commit, and an exception the caller catches inside doesn't undo the inner block's writes.

A transaction belongs to a thread, not an asyncio task: another coroutine's writes on the same thread during an `await` inside the block join it and roll back with it.


## Parameters


`retry: bool = ``False`  
Keep trying to begin while it times out, instead of raising [Timeout](Timeout.md#diskcache_oxide.Timeout). Never retries [TransactionBusy](TransactionBusy.md#diskcache_oxide.TransactionBusy). Each attempt waits up to [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).


## Raises


`TypeError`  
`retry` isn't a `bool`.


## Examples

``` python
with cache.transact() as tx:     # all three land together, or none do
    tx.incr('requests')
    tx.incr(f'requests:{path}')
    tx.set('last_request', now)
```
