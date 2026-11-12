## NullCache.get()


Return `default`, as every entry has already expired. With `expire_time=True` or `tag=True`,


Usage

``` python
NullCache.get(self, key: Key, default: Any = None, expire_time: Literal[False] = False, tag: Literal[False] = False, retry: bool = False) -> Any
 
NullCache.get(self, key: Key, default: Any = None, expire_time: Literal[True], tag: Literal[False] = False, retry: bool = False) -> tuple[Any, float | None]
 
NullCache.get(self, key: Key, default: Any = None, expire_time: Literal[False] = False, tag: Literal[True], retry: bool = False) -> tuple[Any, Key]
 
NullCache.get(self, key: Key, default: Any = None, expire_time: Literal[True], tag: Literal[True], retry: bool = False) -> tuple[Any, float | None, Key]
```


`(default, None)`; with both, `(default, None, None)`.
