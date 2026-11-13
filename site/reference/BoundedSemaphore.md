## BoundedSemaphore


A semaphore shared by every thread and process on the cache: at most `value` holders at once.


Usage

``` python
BoundedSemaphore(
    cache,
    key,
    value=1,
    expire=None,
    tag=None,
)
```


Releasing more than were acquired raises `AssertionError`.


## Methods

| Name | Description |
|----|----|
| [acquire()](#acquire) | Wait for a free slot and take it. |
| [release()](#release) | Give a slot back. |

------------------------------------------------------------------------


#### acquire()


Wait for a free slot and take it.


Usage

``` python
acquire()
```


------------------------------------------------------------------------


#### release()


Give a slot back.


Usage

``` python
release()
```
