## NullCache.reset()


Read [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit), [cull_limit](Cache.cull_limit.md#diskcache_oxide.Cache.cull_limit) or [eviction_policy](Cache.eviction_policy.md#diskcache_oxide.Cache.eviction_policy), or set it (checked as [Cache](Cache.md#diskcache_oxide.Cache) checks it)


Usage

``` python
NullCache.reset(self, key: Literal['size_limit', 'cull_limit'], value: Any = ..., update: bool = True) -> int
 
NullCache.reset(self, key: Literal['eviction_policy'], value: Any = ..., update: bool = True) -> Literal['least-recently-stored', 'none']
 
NullCache.reset(self, key: str, value: Any = ..., update: bool = True) -> int | str
```


and return it.
