## Cache.peekitem()


The newest entry (or with `last=False`, the oldest) of any key, without removing it, as


Usage

``` python
Cache.peekitem(
    last=True,
    *,
    expire_time=False,
    tag=False,
    retry=False,
)
```


diskcache's [peekitem](Cache.peekitem.md#diskcache_oxide.Cache.peekitem). Never writes; expired entries are passed over.


## Returns


`Any`  
`(key, value)`, widened with `expire_time` and `tag` as for [pull()](Cache.pull.md#diskcache_oxide.Cache.pull).


## Raises


`KeyError`  
The cache has no live entry.
