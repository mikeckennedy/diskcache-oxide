## TransactionalCacheLike


A [CacheLike](CacheLike.md#diskcache_oxide.CacheLike) with [transact()](Cache.transact.md#diskcache_oxide.Cache.transact) and the queue calls ([push](Cache.push.md#diskcache_oxide.Cache.push), [pull](Cache.pull.md#diskcache_oxide.Cache.pull), [peek](Cache.peek.md#diskcache_oxide.Cache.peek), [peekitem](Cache.peekitem.md#diskcache_oxide.Cache.peekitem)), which


Usage

``` python
TransactionalCacheLike()
```


[Cache](Cache.md#diskcache_oxide.Cache) and [NullCache](NullCache.md#diskcache_oxide.NullCache) both have and [FanoutCache](FanoutCache.md#diskcache_oxide.FanoutCache) doesn't.

On a [Cache](Cache.md#diskcache_oxide.Cache), `with cache.transact() as tx:` runs the block's calls on this thread in one write transaction; `tx` is the cache. On a [NullCache](NullCache.md#diskcache_oxide.NullCache) it's a no-op that yields the [NullCache](NullCache.md#diskcache_oxide.NullCache).


## Examples

``` python
from diskcache_oxide import TransactionalCacheLike

def count(cache: TransactionalCacheLike, path: str) -> None:
    with cache.transact() as tx:
        tx.incr('requests')
        tx.incr(f'requests:{path}')
```


## Methods

| Name | Description |
|----|----|
| [peek()](#peek) | `(key, value)` at a queue's end, or `default`, without removing it. |
| [peekitem()](#peekitem) | The newest (or oldest) `(key, value)` of any key, without removing it. |
| [pull()](#pull) | Remove and return `(key, value)` from a queue, or `default`. |
| [push()](#push) | Push `value` onto a queue and return its key (None on a durable outage). |
| [transact()](#transact) | A context manager holding a write transaction for the block, yielding the cache. |

------------------------------------------------------------------------


#### peek()


`(key, value)` at a queue's end, or `default`, without removing it.


Usage

``` python
peek(
    prefix=None,
    default=(None, None),
    side="front",
    *,
    expire_time=False,
    tag=False,
    retry=False
)
```


------------------------------------------------------------------------


#### peekitem()


The newest (or oldest) `(key, value)` of any key, without removing it.


Usage

``` python
peekitem(last=True, *, expire_time=False, tag=False, retry=False)
```


------------------------------------------------------------------------


#### pull()


Remove and return `(key, value)` from a queue, or `default`.


Usage

``` python
pull(
    prefix=None,
    default=(None, None),
    side="front",
    *,
    expire_time=False,
    tag=False,
    retry=False
)
```


------------------------------------------------------------------------


#### push()


Push `value` onto a queue and return its key (None on a durable outage).


Usage

``` python
push(self, value: Any, prefix: None = None, side: Literal['front', 'back'] = 'back', expire: float | None = None, tag: Key = None, retry: bool = False) -> int | None
push(self, value: Any, prefix: str, side: Literal['front', 'back'] = 'back', expire: float | None = None, tag: Key = None, retry: bool = False) -> str | None
push(self, value: Any, prefix: str | None = None, side: Literal['front', 'back'] = 'back', expire: float | None = None, tag: Key = None, retry: bool = False) -> int | str | None
```


------------------------------------------------------------------------


#### transact()


A context manager holding a write transaction for the block, yielding the cache.


Usage

``` python
transact(retry=False)
```
