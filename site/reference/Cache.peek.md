## Cache.peek()


The item at `side` of a queue, without removing it, as diskcache's [peek](Cache.peek.md#diskcache_oxide.Cache.peek).


Usage

``` python
Cache.peek(
    prefix=None,
    default=(None, None),
    side="front",
    *,
    expire_time=False,
    tag=False,
    retry=False
)
```


Writes only what [get](Cache.get.md#diskcache_oxide.Cache.get) would (D-055): expired items are passed over and left for [expire()](Cache.expire.md#diskcache_oxide.Cache.expire). Arguments and return values as for [pull()](Cache.pull.md#diskcache_oxide.Cache.pull).
