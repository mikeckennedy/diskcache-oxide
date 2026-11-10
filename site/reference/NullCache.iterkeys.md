## NullCache.iterkeys()


Yield nothing, or with `invalidate=`, that cache's keys, so list-then-delete still works.


Usage

``` python
NullCache.iterkeys(self, prefix: str, reverse: bool = False) -> Iterator[str]
 
NullCache.iterkeys(self, prefix: None = None, reverse: bool = False) -> Iterator[Key]
```
