## CacheLike.get()


Usage

``` python
CacheLike.get(self, key: str, default: Any = None, expire_time: Literal[False] = False) -> Any
 
CacheLike.get(self, key: str, default: Any = None, expire_time: Literal[True]) -> tuple[Any, float | None]
```
