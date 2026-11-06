## FanoutCache.shard_of()


The shard `key` lives in, from 0 to `shards - 1`: the same in every process and release.


Usage

``` python
FanoutCache.shard_of(key)
```


## Examples

``` python
cache.shard_of('user:42') == cache.shard_of('user:42:profile')   # usually False: no grouping by prefix
```
