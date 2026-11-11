## NullCache.push()


Store nothing and return the key an empty queue's first item gets: the item expires the instant


Usage

``` python
NullCache.push(self, value: Any, prefix: None = None, side: Literal['front', 'back'] = 'back', expire: float | None = None, tag: Key = None, retry: bool = False) -> int
 
NullCache.push(self, value: Any, prefix: str, side: Literal['front', 'back'] = 'back', expire: float | None = None, tag: Key = None, retry: bool = False) -> str
 
NullCache.push(self, value: Any, prefix: str | None = None, side: Literal['front', 'back'] = 'back', expire: float | None = None, tag: Key = None, retry: bool = False) -> int | str
```


it's written. Never forwarded to `invalidate=`.
