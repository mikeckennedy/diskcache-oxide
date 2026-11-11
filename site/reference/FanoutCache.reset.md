## FanoutCache.reset()


Read [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit), [cull_limit](Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit) or [eviction_policy](Cache.eviction_policy.md#diskcache_oxide.Cache.eviction_policy) as stored now, or persist a new value and


Usage

``` python
FanoutCache.reset(
    key,
    value=...,
    update=True,
)
```


return it.


## Parameters


`key: bool`  
`'size_limit'`, `'cull_limit'` or `'eviction_policy'`.

`value: Any = …`  
The new value, checked as the constructor checks it; leave it out (or pass `ENOVAL`) to read. On a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) a [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit) is a total, split across the shards.

`update: bool = ``True`  
Must be True: there's no SQLite pragma to skip (diskcache's `update=False`).


## Returns


`int | Literal['least-recently-stored', 'none']`  
The setting: an `int` for a limit, the policy's name for [eviction_policy](Cache.eviction_policy.md#diskcache_oxide.Cache.eviction_policy).


## Raises


`ValueError`  
Another key, `update=False`, or a value the constructor would refuse.

`TypeError`  
A limit that isn't an `int`, or a policy that isn't a `str`.
