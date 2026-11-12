## CacheLike.pop()


Remove `key` and return its value, or `default`.


Usage

``` python
CacheLike.pop(self, key: Key, default: Any = None, expire_time: Literal[False] = False, tag: Literal[False] = False, retry: bool = False) -> Any
 
CacheLike.pop(self, key: Key, default: Any = None, expire_time: Literal[True], tag: Literal[False] = False, retry: bool = False) -> tuple[Any, float | None]
 
CacheLike.pop(self, key: Key, default: Any = None, expire_time: Literal[False] = False, tag: Literal[True], retry: bool = False) -> tuple[Any, Key]
 
CacheLike.pop(self, key: Key, default: Any = None, expire_time: Literal[True], tag: Literal[True], retry: bool = False) -> tuple[Any, float | None, Key]
```
