# Concurrency and fork safety

High-concurrency Python web servers present unique challenges for disk-backed caches: multiple threads accessing a single cache instance, multiple worker processes sharing a directory, and pre-forking application servers (`gunicorn`, `celery`, `uvicorn --workers`).

diskcache-oxide is engineered from the ground up to handle these workloads safely and predictably.


# Free-threaded Python (3.14t and 3.15t)

diskcache-oxide is compiled with `#[pymodule(gil_used = false)]`.

On free-threaded CPython builds, Python's Global Interpreter Lock (GIL) is disabled. Threads run simultaneously across all available CPU cores.

- **No GIL reliance**: diskcache-oxide uses Rust standard synchronization primitives (`std::sync::Mutex`, `Arc`, atomic variables) internally. It never relies on the GIL to prevent race conditions.
- **Detached locks**: Long-running operations (such as waiting on SQLite write locks or reading large file spills) detach the Python thread so they never block garbage collection or other Python threads.
- **Tested in CI**: Every change is continuously tested on Python 3.14, 3.14t, 3.15, and 3.15t.


# Multi-process architecture

Multiple independent Python processes (such as Gunicorn workers or Celery tasks) can safely point to the same cache directory:

- **SQLite WAL Mode**: diskcache-oxide always operates in Write-Ahead Logging (`journal_mode=WAL`) mode. Readers never block writers, and writers never block readers.
- **Cross-process coordination**: Writers coordinate through SQLite's internal WAL locks (`-shm` and `-wal` files).
- **Atomic sequence numbers**: Every write receives a monotonically increasing `store_seq` generated inside the SQLite transaction. Eviction, expiration, and clear operations use this sequence to ensure that concurrent writes are never accidentally wiped out by a running maintenance sweep.


# The fork barrier (`os.register_at_fork`)

Pre-forking web servers create worker processes using POSIX `fork()`. When `fork()` is called after a parent process has initialized SQLite connections, standard SQLite libraries can deadlock, corrupt shared locks, or crash. On macOS, using SQLite across a fork can trigger an immediate abort in Apple's system libraries.

diskcache-oxide implements an automatic **process-wide fork barrier**:

    [ Parent Process ]
     Thread 1: In-flight write transaction ───┐
     Thread 2: About to start transaction ────┼── (Blocked by barrier)
     Main Thread: Calls os.fork() ────────────┴── Barrier waits for Thread 1 to commit
                                                  │
                          ┌───────────────────────┴───────────────────────┐
                          ▼                                               ▼
                  [ Parent Process ]                              [ Child Process ]
              Resumes Thread 2 & writers                   Drops inherited connection handles
                                                           Starts fresh connection pool lazily

1.  **Before fork (`before`)**: The barrier releases the Python GIL and waits for all active SQLite transactions in other threads to complete. It temporarily blocks any new transactions from starting.
2.  **After fork in parent (`after_in_parent`)**: Releases the barrier and unblocks waiting writers in the parent.
3.  **After fork in child (`after_in_child`)**: Resets the process generation counter. Inherited SQLite connection pointers are discarded immediately without closing the inherited file descriptors (which would drop POSIX locks held by the parent). The child opens clean, independent connections on its next cache call.

This barrier is registered automatically when `diskcache_oxide` is imported.


# Bounded connection pooling

In python-diskcache, each thread that touched the cache created and retained its own SQLite connection. In an application running short-lived worker threads or high thread counts (e.g. 64 threads), this resulted in hundreds of open file descriptors inside the cache directory, leading to file descriptor exhaustion ("Too many open files" / `EMFILE`).

diskcache-oxide uses a bounded connection pool:

``` python
cache = Cache('/tmp/my-cache', max_connections=8)  # Default: 8
```

- **Fixed pool size**: At most `max_connections` SQLite connections are opened per cache directory in a process.
- **Fast checkout**: Connections are checked out using an uncontended mutex, held for the duration of the query or transaction, and immediately returned to the pool.
- **Bounded file descriptors**: Even with hundreds of concurrent threads, file descriptor usage remains strictly bounded.
