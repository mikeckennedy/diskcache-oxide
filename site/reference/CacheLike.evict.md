## CacheLike.evict()


Remove every entry tagged `tag` that existed when the call started. Returns how many.


Usage

``` python
CacheLike.evict(
    tag,
    *,
    timeout=None,
    retry=False,
)
```
