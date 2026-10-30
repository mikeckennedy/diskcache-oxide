## Caches.expire_all()


Remove expired entries from every cache. Returns how many each removed, or None for a


Usage

``` python
Caches.expire_all(
    *,
    timeout=None,
)
```


cache that couldn't be opened or expired (logged; the rest still run).
