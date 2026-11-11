## Cache.pull()


Remove and return the item at `side` of a queue, as diskcache's [pull](Cache.pull.md#diskcache_oxide.Cache.pull).


Usage

``` python
Cache.pull(
    prefix=None,
    default=(None, None),
    side="front",
    *,
    expire_time=False,
    tag=False,
    retry=False
)
```


Reads and decodes the item first, then deletes exactly that stored version, as [pop](Cache.pop.md#diskcache_oxide.Cache.pop) does, so an item this process can't read stays queued for one that can (D-056): a pickled item on a `pickle=False` handle raises [UnsafeValueError](UnsafeValueError.md#diskcache_oxide.UnsafeValueError), and a newer deploy's value raises, or with `on_decode_error='miss'` returns `default`. An item no build could read is removed and reported, and the pull goes on to the next. Expired items are passed over. Inside a [transact()](Cache.transact.md#diskcache_oxide.Cache.transact) block it joins the transaction, and a rollback puts the item back.


## Parameters


`prefix: str | None = None`  
The queue's name, or None for the int queue.

`default: Any = (None, None)`\  
Returned when the queue is empty (default `(None, None)`).

`side: Literal[``"front", `<span class="st">`"back"``]`</span>` = ``"front"`  
`'front'` (the default) or `'back'`.

`expire_time: bool = ``False`  
Also return the item's expiry time.

`tag: bool = ``False`  
Also return the item's tag.

`retry: bool = ``False`  
As for [get()](Cache.get.md#diskcache_oxide.Cache.get).


## Returns


`Any`  
`(key, value)`, or with `expire_time` and `tag` `((key, value), expire_time, tag)` and the

pairs, as [get()](Cache.get.md#diskcache_oxide.Cache.get)'s tuples; `default`, widened to match, when empty.


## Examples

``` python
key, job = jobs.pull(prefix='transcode')
if key is not None:
    transcode(job)
```
