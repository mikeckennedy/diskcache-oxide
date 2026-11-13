## Averager


A running average stored under `key`: `add(value)`, [get()](Cache.get.md#diskcache_oxide.Cache.get), [pop()](Cache.pop.md#diskcache_oxide.Cache.pop).


Usage

``` python
Averager(
    cache,
    key,
    expire=None,
    tag=None,
)
```


The total and count are updated together in one transaction, so every thread and process adding to it agrees. `None` while nothing has been added.


## Methods

| Name | Description |
|----|----|
| [add()](#add) | Add `value` to the average. |
| [get()](#get) | The current average, or None if nothing has been added. |
| [pop()](#pop) | The current average, removing it. |

------------------------------------------------------------------------


#### add()


Add `value` to the average.


Usage

``` python
add(value)
```


------------------------------------------------------------------------


#### get()


The current average, or None if nothing has been added.


Usage

``` python
get()
```


------------------------------------------------------------------------


#### pop()


The current average, removing it.


Usage

``` python
pop()
```
