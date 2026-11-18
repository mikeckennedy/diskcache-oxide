## DjangoCache.incr()


Add `delta` atomically, across threads and processes, keeping the entry's expiry.


Usage

``` python
DjangoCache.incr(
    key,
    delta=1,
    version=None,
)
```


## Raises


`ValueError`  
The key is missing or expired.

`TypeError`  
The stored value isn't a number (an `int` or a `float`; a stored `bool` doesn't count).
