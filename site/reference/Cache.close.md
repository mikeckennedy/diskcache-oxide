## Cache.close()


Close this handle. With [checkpoint](Cache.checkpoint.md#diskcache_oxide.Cache.checkpoint), a WAL left larger than half of `wal_size_limit`


Usage

``` python
Cache.close(checkpoint=True)
```


is checkpointed first, waiting at most 1 second; a small WAL costs nothing extra.
