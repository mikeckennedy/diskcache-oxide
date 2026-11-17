## Caches.get()


The named plain cache as `caches[name]` returns it, or `default` if no cache by that name


Usage

``` python
Caches.get(self, name: str) -> Optional[TransactionalCacheLike]
 
Caches.get(self, name: str, default: D) -> TransactionalCacheLike | D
```


is registered. One call instead of `name in caches` and then `caches[name]`.

`default` is for a name that was never registered, nothing else: a registered cache that can't open or is disabled still comes back as a [NullCache](NullCache.md#diskcache_oxide.NullCache), as from `caches[name]`. `name` is typed `str` even on a `Caches[Literal[...]]`, since asking about a name that may not be there is the point.


## Raises


`TypeError`  
The cache was registered with `shards=` (use `fanout(name)`), or its settings are invalid.

`ValueError`  
Its settings are invalid.


## Examples

``` python
if (pages := caches.get('pages')) is None:
    raise RuntimeError('the pages cache was not registered')
pages.set('/about', html)
```
