## NullCache.get()


Return `default`, as every entry has already expired. With `expire_time=True`, `(default, None)`.


Usage

``` python
NullCache.get(self, key: str, default: Any = None, expire_time: Literal[False] = False) -> Any
 
NullCache.get(self, key: str, default: Any = None, expire_time: Literal[True]) -> tuple[Any, float | None]
```
