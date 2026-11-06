## FanoutCache.volume()


The total disk use in bytes of every shard (databases plus value files).


Usage

``` python
FanoutCache.volume()
```


Raises on any shard's failure, under `durable=True` too.
