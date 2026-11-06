## Cache.iterkeys()


Iterate over keys in sorted order, optionally only those starting with `prefix`.


Usage

``` python
Cache.iterkeys(
    *,
    prefix=None,
    reverse=False,
)
```


A prefix is an indexed range scan, so it costs time in proportion to the matches, not the size of the cache. Keys are fetched a page at a time with no lock held in between, so deleting keys while iterating is safe. Expired entries not yet removed are included. Like a generator, an iterator serves one thread at a time: `next()` raises `ValueError` while another thread is inside it.


## Parameters


`prefix: str | None = None`  
Only yield keys that start with this string.

`reverse: bool = ``False`  
Yield keys in descending order.


## Returns


`Iterator[str]`  
An iterator of keys.


## Examples

``` python
for key in cache.iterkeys(prefix='user:42:'):   # an indexed range scan, sorted
    cache.delete(key)
newest_first = cache.iterkeys(reverse=True)
```
