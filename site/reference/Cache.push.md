## Cache.push()


Push `value` onto a queue and return its key, as diskcache's [push](Cache.push.md#diskcache_oxide.Cache.push).


Usage

``` python
Cache.push(
    value, prefix=None, side="back", expire=None, *, tag=None, retry=False
)
```


With `prefix=None` the queue's keys are ints, numbered from 500,000,000,000,000; with a prefix, `str` keys `'{prefix}-'` and 15 digits. `side='back'` takes the next number up, `'front'` the next down. Only keys of exactly that shape belong to the queue: a float, an ordinary key that shares the prefix, or a nested prefix's items are ignored (D-054), so use a prefix no other key starts with. The number is found and the item stored in one write transaction, so concurrent pushes in any process never collide, and a push never overwrites a key.


## Parameters


`value: Any`  
The value, as for [set()](Cache.set.md#diskcache_oxide.Cache.set).

`prefix: None = None`  
The queue's name, or None for the int queue.

`side: bool = ``"back"`  
`'back'` (the default) or `'front'`.

`expire: float | None = None`  
Seconds until the item expires, or None for never.

`tag: Key = None`  
The item's tag, as for [set()](Cache.set.md#diskcache_oxide.Cache.set).

`retry: bool = ``False`  
As for [set()](Cache.set.md#diskcache_oxide.Cache.set).


## Returns


`int | None | str | None | int | str | None`  
The item's key. With `durable=True`, None if the write failed (nothing was stored).


## Raises


`OverflowError`  
The queue's numbers are used up on that side (D-057).

`ValueError`  
`side` isn't `'front'` or `'back'`.

`TypeError`  
`prefix` isn't a `str` or None, or as for [set()](Cache.set.md#diskcache_oxide.Cache.set).


## Examples

``` python
jobs = Cache('/var/lib/my-app/state/jobs', timeout=3.0, eviction_policy='none')
jobs.push({'episode': 512}, prefix='transcode')   # 'transcode-500000000000000'
```
