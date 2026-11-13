## Lock


A lock shared by every thread and process on the cache: [acquire()](Lock.md#diskcache_oxide.Lock.acquire), [release()](Lock.md#diskcache_oxide.Lock.release), [locked()](Lock.md#diskcache_oxide.Lock.locked),


Usage

``` python
Lock(
    cache,
    key,
    expire=None,
    tag=None,
)
```


or `with lock:`. Not reentrant (see [RLock](RLock.md#diskcache_oxide.RLock)); any holder of the key can release it.

[acquire()](Lock.md#diskcache_oxide.Lock.acquire) polls every millisecond until it gets the lock. Give it an [expire](Cache.expire.md#diskcache_oxide.Cache.expire) so a holder that dies without releasing doesn't keep it forever.


## Methods

| Name | Description |
|----|----|
| [acquire()](#acquire) | Wait for the lock and take it. |
| [locked()](#locked) | Whether someone holds the lock. |
| [release()](#release) | Release the lock. |

------------------------------------------------------------------------


#### acquire()


Wait for the lock and take it.


Usage

``` python
acquire()
```


------------------------------------------------------------------------


#### locked()


Whether someone holds the lock.


Usage

``` python
locked()
```


------------------------------------------------------------------------


#### release()


Release the lock.


Usage

``` python
release()
```
