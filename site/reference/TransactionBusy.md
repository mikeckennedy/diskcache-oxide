## TransactionBusy


A thread holding one cache's transaction couldn't begin another's, because another thread holds its write


Usage

``` python
TransactionBusy()
```


lock. It fails at once rather than waiting, since the holder could be waiting on a lock this thread holds.
