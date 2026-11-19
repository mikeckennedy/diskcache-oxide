## DjangoCache.make_and_validate_key()


The key as stored (`prefix:version:key`), checked by [validate_key](DjangoCache.validate_key.md#diskcache_oxide.DjangoCache.validate_key).


Usage

``` python
DjangoCache.make_and_validate_key(
    key,
    version=None,
)
```


A `KEY_FUNCTION`'s `str` subclass becomes a `str`; anything else raises `TypeError`.
