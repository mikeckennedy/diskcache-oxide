## NullCache.get()


Return `default`, as every entry has already expired. With `expire_time=True`, `(default, None)`.


Usage

``` python
NullCache.get(self, key: Key, default: Any = None, expire_time: Literal[False] = False, retry: bool = False) -> Any
 
NullCache.get(self, key: Key, default: Any = None, expire_time: Literal[True], retry: bool = False) -> tuple[Any, float | None]
```
