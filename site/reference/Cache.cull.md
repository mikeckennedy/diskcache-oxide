## Cache.cull()


Remove expired entries, then evict the least recently stored until under [size_limit](Cache.size_limit.md#diskcache_oxide.Cache.size_limit).


Usage

``` python
Cache.cull()
```


## Returns


`int`  
How many entries were removed.
