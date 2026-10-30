## CacheLike.checkpoint()


Empty the write-ahead log. True if it was reset, False if other processes kept it busy.


Usage

``` python
CacheLike.checkpoint(
    *,
    timeout=None,
)
```
