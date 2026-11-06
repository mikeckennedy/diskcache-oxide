## FanoutCache.iterkeys()


Iterate over keys in sorted order, optionally only those starting with `prefix`.


Usage

``` python
FanoutCache.iterkeys(
    *,
    prefix=None,
    reverse=False,
)
```


The same order [Cache.iterkeys](Cache.iterkeys.md#diskcache_oxide.Cache.iterkeys) gives for the same keys (code-point order, like `sorted()`): a merge of every shard's sorted keys. Each shard keeps a small page of keys buffered, so the first `next()` reads every shard once, and later ones read a shard only when its page runs out. No connection or lock is held between `next()` calls. As on [Cache](Cache.md#diskcache_oxide.Cache), it isn't a snapshot: a key stored or deleted while it runs may or may not be yielded, but none is yielded twice and the order holds. Expired entries not yet removed are included. `next()` raises `ValueError` while another thread is inside it.


## Parameters


`prefix: str | None = None`  
Only yield keys that start with this string.

`reverse: bool = ``False`  
Yield keys in descending order.


## Raises


`Timeout`  
A shard stayed locked, or a fork stayed pending, for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout) during one `next()` (which shares that one budget across the shards it reads).
