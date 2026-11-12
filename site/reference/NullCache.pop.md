## NullCache.pop()


Return `default`, shaped as [get()](Cache.get.md#diskcache_oxide.Cache.get) returns it. With `invalidate=`, also delete the key from


Usage

``` python
NullCache.pop(self, key: Key, default: Any = None, expire_time: Literal[False] = False, tag: Literal[False] = False, retry: bool = False) -> Any
 
NullCache.pop(self, key: Key, default: Any = None, expire_time: Literal[True], tag: Literal[False] = False, retry: bool = False) -> tuple[Any, float | None]
 
NullCache.pop(self, key: Key, default: Any = None, expire_time: Literal[False] = False, tag: Literal[True], retry: bool = False) -> tuple[Any, Key]
 
NullCache.pop(self, key: Key, default: Any = None, expire_time: Literal[True], tag: Literal[True], retry: bool = False) -> tuple[Any, float | None, Key]
```


that cache.
