## Caches.\_\_getitem\_\_()


The named cache: opened on first use, then the same object every time.


Usage

``` python
Caches.__getitem__(name)
```


A [NullCache](NullCache.md#diskcache_oxide.NullCache) while it can't be opened (see the class docs), or `NullCache(invalidate=real)` while it's disabled.


## Raises


`KeyError`  
No cache by that name is registered.

`(TypeError, ValueError)`  
Its settings are invalid.
