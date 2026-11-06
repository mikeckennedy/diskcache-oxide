## FanoutCache.\_\_len\_\_()


The number of entries in every shard, including expired entries not yet removed.


Usage

``` python
FanoutCache.__len__()
```


Raises on any shard's failure, under `durable=True` too.
