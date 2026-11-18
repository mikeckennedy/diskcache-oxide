## DjangoCache.cache


The [Cache](Cache.md#diskcache_oxide.Cache) (or [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache)) under this backend, shared by every alias on its directory.


`DjangoCache.cache: Cache | FanoutCache | NullCache`


Its keys are the made keys (`prefix:version:key`), not the ones passed to the backend. On a `durable=True` alias whose directory couldn't be opened, a [NullCache](NullCache.md#diskcache_oxide.NullCache) until the next try.
