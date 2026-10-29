## NullCache.incr()


Return `default + delta`, since every count starts again from `default`.


Usage

``` python
NullCache.incr(
    key,
    delta=1,
    default=0,
)
```


## Raises


`KeyError`  
`default` is None.
