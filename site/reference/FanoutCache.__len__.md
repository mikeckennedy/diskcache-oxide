## FanoutCache.\_\_len\_\_()


The number of live entries in every shard: expired ones don't count, as on [Cache](Cache.md#diskcache_oxide.Cache).


Usage

``` python
FanoutCache.__len__()
```


Raises on any shard's failure, under `durable=True` too.
