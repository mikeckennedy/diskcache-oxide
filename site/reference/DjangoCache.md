## DjangoCache


A Django cache backend on a diskcache-oxide [Cache](Cache.md#diskcache_oxide.Cache) (or a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) with `OPTIONS['shards']`).


Usage

``` python
DjangoCache(
    location,
    params,
)
```


`'BACKEND': 'diskcache_oxide.DjangoCache'`. `OPTIONS` takes [Cache](Cache.md#diskcache_oxide.Cache)'s keyword names; the defaults are a 1.0 s lock wait (`OPTIONS['timeout']`) and `pickle=True`. Errors raise ([Timeout](Timeout.md#diskcache_oxide.Timeout) for a held lock) unless `OPTIONS['durable']` is set, which drops single-key writes instead. Every instance and alias on a directory shares one handle per process, and [close()](Cache.close.md#diskcache_oxide.Cache.close) does nothing. See the Django cache backend guide for the recommended setup.


## Parameters


`location: str | os.PathLike[str]`  
The cache directory, Django's `LOCATION`. Required.

`params: dict[str, Any]`  
The rest of the alias's settings, as Django passes them.


## Raises


`ImproperlyConfigured`  
No `LOCATION`, an unknown or refused option, another alias on the same directory with different `OPTIONS`, or a `durable=True` alias that the cache or cached_db session engine stores sessions in.
