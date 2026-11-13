## throttle()


A decorator allowing at most `count` calls every `seconds`, across every thread and process on


Usage

``` python
throttle(
    cache,
    count,
    seconds,
    name=None,
    expire=None,
    tag=None,
    time_func=time.time,
    sleep_func=time.sleep
)
```


the cache: a token bucket stored under `name` (the function's qualified name by default). A call with no token waits (`sleep_func`) until one is due.
