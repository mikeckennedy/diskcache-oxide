## FanoutCache.memoize()


Return a decorator that caches the function's results in this cache, as diskcache's [memoize](Cache.memoize.md#diskcache_oxide.Cache.memoize).


Usage

``` python
FanoutCache.memoize(
    name=None,
    typed=False,
    expire=None,
    tag=None,
    ignore=(),
)
```


A call's key is `(name,) + args + (None,)` and then each keyword argument's name and value, sorted by name, where `name` defaults to the function's module and qualified name: the same tuple diskcache builds, which `wrapper.__cache_key__(*args, **kwargs)` returns. Arguments aren't bound to the signature, so `f(1, 2)` and `f(1, b=2)` are two entries. A miss runs the function and stores its result (`None` included; an exception stores nothing); reads and writes pass `retry=True`. Call it: `@cache.memoize()`, not `@cache.memoize`.


## Parameters


`name: str | None = None`  
The key's first element, in place of the function's qualified name.

`typed: bool = ``False`  
Also key on each argument's type, as `'module.qualname'` (D-045), so `f(1)` and `f(1.0)` are two entries.

`expire: float | None = None`  
Seconds until a stored result expires; `None` never. Zero or negative never stores, as in diskcache.

`tag: Key = None`  
The tag every stored result gets, for `evict(tag)`: a key type, checked here.

`ignore: Iterable[int | str] = ()`\  
Positions and keyword names left out of the key.


## Raises


`TypeError`  
`name` is callable (the decorator was used without parentheses), `tag` isn't a key type, the function is `async def`, or, at a call, an argument isn't a key type (D-048).

`ValueError`  
at a call, an argument is a NaN or a tuple nested too deep (D-048).


## Examples

``` python
@cache.memoize(expire=300)
def episode(number: int) -> dict:
    return load_episode(number)

cache.delete(episode.__cache_key__(42))  # forget one call
```
