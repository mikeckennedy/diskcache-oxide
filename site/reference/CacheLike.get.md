## CacheLike.get()


Return the value for `key`, or `default` if it's missing or expired.


Usage

``` python
CacheLike.get(self, key: str, default: Any = None, expire_time: Literal[False] = False) -> Any
 
CacheLike.get(self, key: str, default: Any = None, expire_time: Literal[True]) -> tuple[Any, float | None]
```
