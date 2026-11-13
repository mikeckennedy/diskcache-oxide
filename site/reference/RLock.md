## RLock


A reentrant lock shared by every thread and process on the cache: the thread holding it may


Usage

``` python
RLock(
    cache,
    key,
    expire=None,
    tag=None,
)
```


take it again, and must release it as many times. Another thread or process waits.


## Methods

| Name | Description |
|----|----|
| [acquire()](#acquire) | Wait for the lock, or take it again if this thread holds it. |
| [release()](#release) | Release once. Raises `AssertionError` if this thread doesn't hold the lock. |

------------------------------------------------------------------------


#### acquire()


Wait for the lock, or take it again if this thread holds it.


Usage

``` python
acquire()
```


------------------------------------------------------------------------


#### release()


Release once. Raises `AssertionError` if this thread doesn't hold the lock.


Usage

``` python
release()
```
