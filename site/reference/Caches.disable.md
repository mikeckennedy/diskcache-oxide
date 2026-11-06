## Caches.disable()


Later lookups of these caches return `NullCache(invalidate=real)`.


Usage

``` python
Caches.disable(*names)
```


## Examples

``` python
caches.disable('pages')       # reads miss, writes are dropped, deletes still land
caches['pages'].get('/about') # None
caches.enable('pages')
```
