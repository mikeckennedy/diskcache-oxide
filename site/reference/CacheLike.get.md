## CacheLike.get()


Return the value for `key`, or `default` if it's missing or expired.


Usage

``` python
CacheLike.get(self, key: Key, default: Any = None, expire_time: Literal[False] = False, tag: Literal[False] = False, retry: bool = False) -> Any
 
CacheLike.get(self, key: Key, default: Any = None, expire_time: Literal[True], tag: Literal[False] = False, retry: bool = False) -> tuple[Any, float | None]
 
CacheLike.get(self, key: Key, default: Any = None, expire_time: Literal[False] = False, tag: Literal[True], retry: bool = False) -> tuple[Any, Key]
 
CacheLike.get(self, key: Key, default: Any = None, expire_time: Literal[True], tag: Literal[True], retry: bool = False) -> tuple[Any, float | None, Key]
```
