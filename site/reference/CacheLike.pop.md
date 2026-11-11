## CacheLike.pop()


Remove `key` and return its value, or `default`.


Usage

``` python
CacheLike.pop(self, key: Key, default: Any = None, expire_time: Literal[False] = False, retry: bool = False) -> Any
 
CacheLike.pop(self, key: Key, default: Any = None, expire_time: Literal[True], retry: bool = False) -> tuple[Any, float | None]
```
