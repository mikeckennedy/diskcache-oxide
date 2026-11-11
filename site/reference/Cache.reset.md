## Cache.reset()


Read [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit) or [cull_limit](Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit) as stored now, or persist a new value and return it.


Usage

``` python
Cache.reset(
    key,
    value=...,
    update=True,
)
```


## Parameters


`key: str`  
`'size_limit'` or `'cull_limit'`.

`value: Any = …`  
The new value, checked as the constructor checks it; leave it out (or pass `ENOVAL`) to read. On a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) a [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit) is a total, split across the shards.

`update: bool = ``True`  
Must be True: there's no SQLite pragma to skip (diskcache's `update=False`).


## Raises


`ValueError`  
Another key, `update=False`, or a value the constructor would refuse.

`TypeError`  
A value that isn't an `int`.
