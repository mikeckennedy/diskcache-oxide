## NullCache.pop()


Return `default` (`(default, None)` with `expire_time=True`). With `invalidate=`, also delete


Usage

``` python
NullCache.pop(self, key: Key, default: Any = None, expire_time: Literal[False] = False, retry: bool = False) -> Any
 
NullCache.pop(self, key: Key, default: Any = None, expire_time: Literal[True], retry: bool = False) -> tuple[Any, float | None]
```


the key from that cache.
