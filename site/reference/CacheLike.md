## CacheLike


What application code can rely on from a [Cache](Cache.md#diskcache_oxide.Cache), a [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) or a [NullCache](NullCache.md#diskcache_oxide.NullCache).


Usage

``` python
CacheLike()
```


All three satisfy it, so a factory can return either and callers never need to check which one they got. A test keeps the three signatures identical.


## Examples

``` python
from diskcache_oxide import Cache, CacheLike, NullCache

def make_cache(enabled: bool) -> CacheLike:
    return Cache('/var/cache/my-app') if enabled else NullCache()

def load_user(cache: CacheLike, user_id: str) -> dict:
    if (user := cache.get(f'user:{user_id}')) is not None:
        return user
    user = db.load_user(user_id)
    cache.set(f'user:{user_id}', user, expire=300)
    return user
```
