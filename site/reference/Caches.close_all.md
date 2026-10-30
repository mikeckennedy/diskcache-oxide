## Caches.close_all()


Close every opened cache and forget it; a later lookup opens it again.


Usage

``` python
Caches.close_all()
```


A lookup that races this can return a handle that's no longer tracked. It still works: a closed handle reopens when it's used.
