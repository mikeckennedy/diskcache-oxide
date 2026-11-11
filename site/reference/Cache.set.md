## Cache.set()


Store `value` under `key`, replacing any existing value.


Usage

``` python
Cache.set(
    key,
    value,
    expire=None,
    *,
    tag=None,
    retry=False,
)
```


Values of 64 KiB or more (after encoding) are stored in their own file.


## Parameters


`key: Key`  
The key.

`value: Any`  
The value: `str`, `bytes`, numbers, `Decimal`, `bool`, `None`, and lists, tuples, dicts, sets, frozensets, datetimes and dates of those, or anything picklable with `pickle=True`.

`expire: float | None = None`  
Seconds until the entry expires, or None for never.

`tag: Key = None`  
A label for the entry, which `evict(tag)` removes by: any value a key can be, compared by exact type, so `1`, `1.0` and `True` are three tags. None for none. An overwrite replaces the old tag, so an overwrite without one leaves the entry untagged.

`retry: bool = ``False`  
On a [Timeout](Timeout.md#diskcache_oxide.Timeout), try again until the call gets through, each round with the whole [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout), a `durable=True` handle included. Only [Timeout](Timeout.md#diskcache_oxide.Timeout) is retried.


## Returns


`bool`  
True. With `durable=True`, False if the write failed and was dropped: whatever was

stored under `key` stays, and can be served stale.


## Raises


`TypeError`  
The value can't be stored exactly without pickle, [expire](Cache.expire.md#diskcache_oxide.Cache.expire) is a `bool`, or `tag` isn't a valid key type.

`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).


## Examples

``` python
cache.set('episodes:popular', episodes, expire=300)   # expires in 5 minutes
cache.set('user:42:profile', profile, tag='user:42')  # evict('user:42') removes it
cache.set('config', {'retries': 3})                   # never expires
```
