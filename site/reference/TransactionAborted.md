## TransactionAborted


A held transaction didn't commit and can't: it was rolled back (after an error inside it, or by another thread


Usage

``` python
TransactionAborted()
```


or an earlier exit), so nothing written in it will be committed. Raised by a write inside a transaction SQLite already rolled back (a full disk, an I/O error), and by a `with` block's exit, with no exception passing, when the block didn't commit. A `RuntimeError`, so code that catches that keeps working.
