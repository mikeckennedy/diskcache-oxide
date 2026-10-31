## Cache.incr()


Add `delta` to the number stored under `key`, atomically, and return the result.


Usage

``` python
Cache.incr(
    key,
    delta=...,
    default=...,
)
```


Atomic across threads and processes, so concurrent increments are never lost. The entry keeps its existing expiry.


## Parameters


`key: str`  
The key.

`delta: int | float = …`  
The amount to add (default 1).

`default: int | float | None = …`  
The starting value for a missing or expired key (default 0). Pass None to raise `KeyError` instead.


## Returns


`int | float`  
The new value. With `durable=True`, `default + delta` if the call failed, whatever was

stored, so a rate limiter fails open. Don't use that number as an id: two callers can

get the same one.


## Raises


`KeyError`  
The key is missing and `default` is None, or with `durable=True`, the call failed and `default` is None.

`TypeError`  
The stored value isn't a number.

`Timeout`  
Another process held the write lock for longer than [timeout](Cache.timeout.md#diskcache_oxide.Cache.timeout).
