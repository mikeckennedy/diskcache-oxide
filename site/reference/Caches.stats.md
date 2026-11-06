## Caches.stats()


Count and volume for every registered cache, opening each one. Never raises for a cache


Usage

``` python
Caches.stats()
```


that can't be read: its `error` says why.


## Examples

``` python
for name, stat in caches.stats().items():
    print(name, stat.count, stat.volume, stat.error or 'ok')
```
