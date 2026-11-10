## Cache.decr()


Subtract `delta` from the number stored under `key`, atomically. See [incr()](Cache.incr.md#diskcache_oxide.Cache.incr).


Usage

``` python
Cache.decr(
    key,
    delta=...,
    default=...,
    *,
    retry=False,
)
```


## Returns


`int | float`  
The new value.
