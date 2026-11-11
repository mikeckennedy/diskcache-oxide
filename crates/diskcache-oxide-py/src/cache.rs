//! `diskcache_oxide.Cache`: the frozen pyclass over a shared core `Store` (04-design.md section 2).
//!
//! The class is the public object itself, with no Python wrapper frame (Spike 2). Every operation:
//! validates and encodes its arguments, reads the clock once (`clock=` or the native clock), and
//! only then calls into the store, which takes the write lock, gate and connection. Decoding a
//! small value happens while the connection is held, straight from SQLite's row buffer, and runs
//! no user code.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Condvar, Mutex, PoisonError, TryLockError};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use std::cell::Cell;
use std::collections::{HashSet, VecDeque};

use diskcache_oxide_core::key::{Key, OwnedKey};
use diskcache_oxide_core::maintenance::{Cursor, Interrupted, Order, Snapshot};
use diskcache_oxide_core::open::{Requested, Role};
use diskcache_oxide_core::ops::{Hit, Number, Pop, Value, Version, VersionDelete};
use diskcache_oxide_core::rows::Tag;
use diskcache_oxide_core::schema::{DEFAULT_SIZE_LIMIT, EvictionPolicy};
use diskcache_oxide_core::store::{
    DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, HeldTransaction, OpenOptions, Store,
};
use diskcache_oxide_core::{Error, ValueRef, lockwait, spill};
use pyo3::PyTraverseError;
use pyo3::exceptions::{
    PyException, PyKeyError, PyMemoryError, PyOSError, PyOverflowError, PyRecursionError, PyRuntimeError,
    PyTimeoutError, PyTypeError, PyValueError,
};
use pyo3::gc::PyVisit;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyBool, PyFloat, PyInt, PyString, PyTuple};

use crate::codec::{self, Codec, Payload, Serializer, TypeRegistry};
use crate::durable::{Failures, Op};
use crate::to_py_err;

mod fanout;
pub(crate) use fanout::{_open_fanout_keeping_stored, FanoutCache};

/// A disk cache backed by SQLite, shared safely across threads and processes.
#[pyclass(frozen, module = "diskcache_oxide")]
pub(crate) struct Cache {
    handle: StoreHandle,
    config: HandleConfig,
}

/// This handle's membership in one store: the store, its shard index (0 for a plain `Cache`), and
/// whether this handle closed it. A `FanoutCache` holds one per shard.
struct StoreHandle {
    store: Arc<Store>,
    shard: usize,
    closed: AtomicBool,
}

/// What a handle applies to every store it operates on: its settings and what it has reported.
/// Embedded by value, so an operation reaches it with no indirection. The operations that run
/// against one store take that store explicitly, so a handle over several stores shares them.
struct HandleConfig {
    /// The directory the handle was opened on: shown in `repr` and named in failure reports.
    directory: String,
    timeout: Duration,
    codec: Codec,
    clock: Option<Py<PyAny>>,
    on_decode_error: DecodePolicy,
    /// `durable=True`: what this handle has swallowed. `None` means failures raise.
    failures: Option<Box<Failures>>,
    /// Stored versions already reported under `on_decode_error`, by shard: a `Version` repeats
    /// across a `FanoutCache`'s shards, so one shard's report mustn't silence another's (see
    /// `first_report`).
    reported: Mutex<HashSet<(usize, Version)>>,
    /// What pickling the handle rebuilds it from: the absolute directory and the options the handle
    /// was opened with, as passed (settings persisted in the directory aren't carried).
    reopen: Py<pyo3::types::PyDict>,
}

/// How many reported versions a handle remembers before it forgets them all and starts over.
const REPORTED_VERSIONS: usize = 1024;

#[pymethods]
impl Cache {
    #[new]
    #[pyo3(signature = (
        directory,
        timeout = None,
        *,
        size_limit = None,
        cull_limit = None,
        eviction_policy = None,
        serializer = "msgpack",
        pickle = false,
        types = None,
        clock = None,
        max_connections = None,
        sqlite_cache_size = None,
        wal_size_limit = None,
        wal_checkpoint = "manual",
        on_decode_error = None,
        durable = false,
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        directory: PathBuf,
        timeout: Option<&Bound<'_, PyAny>>,
        size_limit: Option<&Bound<'_, PyAny>>,
        cull_limit: Option<&Bound<'_, PyAny>>,
        eviction_policy: Option<&str>,
        serializer: &str,
        pickle: bool,
        types: Option<Py<TypeRegistry>>,
        clock: Option<Py<PyAny>>,
        max_connections: Option<&Bound<'_, PyAny>>,
        sqlite_cache_size: Option<&Bound<'_, PyAny>>,
        wal_size_limit: Option<&Bound<'_, PyAny>>,
        wal_checkpoint: &str,
        on_decode_error: Option<DecodePolicy>,
        durable: bool,
    ) -> PyResult<Self> {
        let reopen = ReopenOptions {
            timeout,
            serializer,
            pickle,
            types: types.as_ref(),
            clock: clock.as_ref(),
            max_connections,
            sqlite_cache_size,
            wal_size_limit,
            wal_checkpoint,
            on_decode_error: on_decode_error.as_ref(),
            durable,
        }
        .build(py, &directory)?;
        let settings = Settings::parse(
            py,
            timeout,
            size_limit,
            cull_limit,
            eviction_policy,
            serializer,
            max_connections,
            sqlite_cache_size,
            wal_size_limit,
            wal_checkpoint,
        )?;
        let max_connections = settings.max_connections.unwrap_or(DEFAULT_MAX_CONNECTIONS);
        let options = settings.options(directory.clone(), max_connections, Role::Plain);
        let store = crate::detach(py, || Store::open(&options)).map_err(to_py_err)?;
        Ok(Self {
            handle: StoreHandle { store, shard: 0, closed: AtomicBool::new(false) },
            config: HandleConfig::new(&directory, &settings, pickle, types, clock, on_decode_error, durable, reopen),
        })
    }

    // -- attributes ---------------------------------------------------------------------------

    #[getter]
    fn directory(&self) -> &str {
        &self.config.directory
    }

    #[getter]
    fn timeout(&self) -> f64 {
        self.config.timeout.as_secs_f64()
    }

    #[getter]
    fn size_limit(&self) -> i64 {
        self.handle.store.cached_limits().size_limit
    }

    #[getter]
    fn cull_limit(&self) -> i64 {
        self.handle.store.cached_limits().cull_limit
    }

    /// 'least-recently-stored' or 'none', as this handle last saw it stored. Every write reads the
    /// stored one afresh, so another process's `reset('eviction_policy', ...)` takes effect at once.
    #[getter]
    fn eviction_policy(&self) -> &'static str {
        self.handle.store.cached_limits().eviction_policy.name()
    }

    /// Always True for a real cache (a `NullCache` is False).
    #[getter]
    #[allow(clippy::unused_self)] // A Python attribute: it needs `self` even though it's constant.
    fn enabled(&self) -> bool {
        true
    }

    /// Test hook: what forced WAL checkpoints have done on this cache's store.
    fn _wal_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        let s = self.handle.store.wal_stats();
        let d = pyo3::types::PyDict::new(py);
        for (k, v) in [
            ("checks", s.checks),
            ("attempts", s.attempts),
            ("successes", s.successes),
            ("busy", s.busy),
            ("errors", s.errors),
            ("no_slot", s.no_slot),
            ("fork_pending", s.fork_pending),
            ("max_pause_us", s.max_pause_us),
            ("max_passive_us", s.max_passive_us),
            ("max_truncate_us", s.max_truncate_us),
            ("slow", s.slow),
            ("last_wal_bytes", s.last_wal_bytes),
        ] {
            d.set_item(k, v)?;
        }
        Ok(d)
    }

    /// Test hook: every failure `durable=True` has swallowed on this handle, by bucket (`busy`,
    /// `broken`, `lost_deletes`) and then by operation. Empty on a handle that isn't durable.
    fn _durable_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        match &self.config.failures {
            Some(failures) => failures.stats(py),
            None => Ok(pyo3::types::PyDict::new(py)),
        }
    }

    /// Test hook: how many open handles this cache's store counts, deferred releases included.
    fn _handle_count(&self) -> isize {
        self.handle.store.handles()
    }

    fn __repr__(&self) -> String {
        format!("Cache({:?})", self.config.directory)
    }

    /// Lets the garbage collector see the Python objects this handle holds: a `clock` or an
    /// `on_decode_error` callable that refers back to the cache would otherwise leak both.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.config.traverse(&visit)
    }

    /// Pickles as the directory and the options this handle was opened with, so unpickling (in
    /// another process, say) opens the same cache. A `clock` or `on_decode_error` that doesn't
    /// pickle makes the cache not pickle either.
    fn __reduce__(&self, py: Python<'_>) -> PyResult<Reduced> {
        let rebuild = py.import("diskcache_oxide._pickling")?.getattr("rebuild")?.unbind();
        Ok((rebuild, ("Cache", self.config.reopen.bind(py).copy()?.unbind())))
    }

    /// True for a `durable=True` handle.
    #[getter]
    fn durable(&self) -> bool {
        self.config.failures.is_some()
    }

    /// For the recipes: whether this thread holds a transaction on this cache, inside which a recipe
    /// that waited would hold the write lock and could never be released by another process.
    fn _in_transaction(&self) -> bool {
        self.handle.store.held_by_this_thread()
    }

    /// Checks the database and the value files against each other, as diskcache's `check()`: a list
    /// of `warnings.WarningMessage`. With `fix`, repairs what it can and `VACUUM`s.
    #[pyo3(signature = (fix = false, retry = false))]
    fn check(&self, py: Python<'_>, fix: bool, retry: bool) -> PyResult<Py<pyo3::types::PyList>> {
        let handle = &self.handle;
        // One list across retry rounds, so what an earlier round fixed stays reported, once.
        let mut found = Vec::new();
        loop {
            let started = Instant::now();
            let round = crate::detach(py, || {
                handle.run(self.config.timeout, |timeout| handle.store.check(fix, timeout, &mut found))
            });
            match round {
                Ok(()) => break,
                Err(Error::Timeout { .. }) if retry => pause_before_retry(py, started)?,
                Err(error) => return Err(to_py_err(error)),
            }
        }
        findings_to_warnings(py, found)
    }

    /// `(hits, misses)` of `get` and `cache[key]` in this process, as they were; then resets them
    /// with `reset`, and turns counting on or off with `enable`, as diskcache's `stats()`.
    #[pyo3(signature = (enable = true, reset = false))]
    fn stats(&self, enable: bool, reset: bool) -> (u64, u64) {
        self.handle.store.stats().take(enable, reset)
    }

    /// Reads `size_limit`, `cull_limit` or `eviction_policy` as stored now, or with `value` persists
    /// it (validated as the constructor does) and returns it.
    #[pyo3(signature = (key, value = None, update = true))]
    fn reset(&self, py: Python<'_>, key: &str, value: Option<&Bound<'_, PyAny>>, update: bool) -> PyResult<Py<PyAny>> {
        let which = ResetLimit::parse(key, value, update)?;
        let handle = &self.handle;
        let limits = crate::detach(py, || {
            handle.run(self.config.timeout, |timeout| {
                if which.is_read() {
                    handle.store.read_limits(timeout)
                } else {
                    handle.store.set_limits(timeout, which.requested())
                }
            })
        })
        .map_err(to_py_err)?;
        which.of(py, limits)
    }

    // -- reads --------------------------------------------------------------------------------

    #[pyo3(signature = (key, default = None, *, expire_time = false, tag = false, retry = false))]
    fn get(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        default: Option<Py<PyAny>>,
        expire_time: bool,
        tag: bool,
        retry: bool,
    ) -> PyResult<Py<PyAny>> {
        let k = codec::key::encode_key(key)?;
        let wants = Wants { expire_time, tag };
        retrying(py, retry, || {
            self.config.get(py, &self.handle, k.key(), default.as_ref().map(|d| d.clone_ref(py)), wants)
        })
    }

    fn __getitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let k = codec::key::encode_key(key)?;
        retrying(py, false, || self.config.get_item(py, &self.handle, key, k.key()))
    }

    fn __contains__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        let k = codec::key::encode_key(key)?;
        retrying(py, false, || self.config.contains(py, &self.handle, k.key()))
    }

    fn __len__(&self) -> PyResult<usize> {
        let count =
            self.handle.run(self.config.timeout, |timeout| self.handle.store.len(timeout)).map_err(to_py_err)?;
        Ok(usize::try_from(count).unwrap_or(0))
    }

    /// Always True: a cache is usable even when empty (D-009).
    #[allow(clippy::unused_self)] // A Python protocol method: it needs `self`.
    fn __bool__(&self) -> bool {
        true
    }

    fn volume(&self) -> PyResult<i64> {
        self.handle.run(self.config.timeout, |timeout| self.handle.store.volume(timeout)).map_err(to_py_err)
    }

    /// Checkpoints the write-ahead log and truncates it to zero (D-024), retrying until `timeout`.
    /// True if the WAL was reset; False if readers or writers in other processes kept it busy.
    /// `timeout`: how long to keep trying, in seconds (default: the cache's timeout).
    #[pyo3(signature = (*, timeout = None))]
    fn checkpoint(&self, py: Python<'_>, timeout: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
        let timeout = self.config.call_timeout(timeout)?;
        // A closed handle that can't rejoin behind a fork is busy too, and this never raises for busy.
        let timeout = match self.handle.ensure_open(timeout) {
            Ok(left) => left,
            Err(Error::Timeout { .. }) => return Ok(false),
            Err(error) => return Err(to_py_err(error)),
        };
        // Each round waits on the write lock and backs off after a busy one. Detached, none of
        // those waits takes the interpreter back in between (the copy itself always runs detached).
        crate::detach(py, || self.handle.store.checkpoint(timeout)).map_err(to_py_err)
    }

    // -- writes -------------------------------------------------------------------------------

    #[pyo3(signature = (key, value, expire = None, *, tag = None, retry = false))]
    fn set(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        expire: Option<&Bound<'_, PyAny>>,
        tag: Option<&Bound<'_, PyAny>>,
        retry: bool,
    ) -> PyResult<bool> {
        let k = codec::key::encode_key(key)?;
        let t = codec::key::encode_tag(tag)?;
        let (encoded, expire) = self.config.encode_value(py, value, expire)?;
        let tag = t.as_ref().map(|t| Tag(t.key()));
        retrying(py, retry, || self.config.set(py, &self.handle, k.key(), &encoded, expire, tag))
    }

    fn __setitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        self.set(py, key, value, None, None, false).map(|_| ())
    }

    #[pyo3(signature = (key, value, expire = None, *, tag = None, retry = false))]
    fn add(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        expire: Option<&Bound<'_, PyAny>>,
        tag: Option<&Bound<'_, PyAny>>,
        retry: bool,
    ) -> PyResult<bool> {
        let k = codec::key::encode_key(key)?;
        let t = codec::key::encode_tag(tag)?;
        let (encoded, expire) = self.config.encode_value(py, value, expire)?;
        let tag = t.as_ref().map(|t| Tag(t.key()));
        retrying(py, retry, || self.config.add(py, &self.handle, k.key(), &encoded, expire, tag))
    }

    #[pyo3(signature = (key, *, retry = false))]
    fn delete(&self, py: Python<'_>, key: &Bound<'_, PyAny>, retry: bool) -> PyResult<bool> {
        let k = codec::key::encode_key(key)?;
        retrying(py, retry, || self.config.delete(py, &self.handle, k.key()))
    }

    fn __delitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<()> {
        if self.delete(py, key, false)? { Ok(()) } else { Err(PyKeyError::new_err(key.clone().unbind())) }
    }

    #[pyo3(signature = (key, default = None, *, expire_time = false, tag = false, retry = false))]
    fn pop(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        default: Option<Py<PyAny>>,
        expire_time: bool,
        tag: bool,
        retry: bool,
    ) -> PyResult<Py<PyAny>> {
        let k = codec::key::encode_key(key)?;
        let wants = Wants { expire_time, tag };
        retrying(py, retry, || {
            self.config.pop(py, &self.handle, k.key(), default.as_ref().map(|d| d.clone_ref(py)), wants)
        })
    }

    #[pyo3(signature = (key, expire = None, *, retry = false))]
    fn touch(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        expire: Option<&Bound<'_, PyAny>>,
        retry: bool,
    ) -> PyResult<bool> {
        let k = codec::key::encode_key(key)?;
        retrying(py, retry, || self.config.touch(py, &self.handle, k.key(), expire))
    }

    #[pyo3(signature = (key, delta = Delta(Number::Int(1)), default = IncrDefault(Some(Number::Int(0))), *, retry = false))]
    fn incr(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        delta: Delta,
        default: IncrDefault,
        retry: bool,
    ) -> PyResult<Py<PyAny>> {
        let k = codec::key::encode_key(key)?;
        retrying(py, retry, || self.config.add_number(py, &self.handle, key, k.key(), delta, default, false))
    }

    #[pyo3(signature = (key, delta = Delta(Number::Int(1)), default = IncrDefault(Some(Number::Int(0))), *, retry = false))]
    fn decr(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        delta: Delta,
        default: IncrDefault,
        retry: bool,
    ) -> PyResult<Py<PyAny>> {
        let k = codec::key::encode_key(key)?;
        retrying(py, retry, || self.config.add_number(py, &self.handle, key, k.key(), delta, default, true))
    }

    /// A decorator that caches a function's results in this cache (diskcache's `memoize`). The logic
    /// is pure Python (`diskcache_oxide._memoize`); a compiled class can't gain a method from Python.
    #[pyo3(
        signature = (name = None, typed = false, expire = None, tag = None, ignore = None),
        text_signature = "($self, name=None, typed=False, expire=None, tag=None, ignore=())"
    )]
    fn memoize(
        slf: &Bound<'_, Self>,
        name: Option<&Bound<'_, PyAny>>,
        typed: bool,
        expire: Option<&Bound<'_, PyAny>>,
        tag: Option<&Bound<'_, PyAny>>,
        ignore: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<Py<PyAny>> {
        let durable = slf.get().config.failures.is_some();
        memoize_on(slf.as_any(), name, typed, expire, tag, ignore, durable)
    }

    // -- maintenance and iteration -------------------------------------------------------------

    /// Removes every expired entry. Returns how many it removed. `timeout` overrides the cache's
    /// for this call: how long each chunk may wait for the write lock.
    #[pyo3(signature = (*, timeout = None, retry = false))]
    fn expire(&self, py: Python<'_>, timeout: Option<&Bound<'_, PyAny>>, retry: bool) -> PyResult<u64> {
        let timeout = self.config.call_timeout(timeout)?;
        let now = self.config.now(py)?;
        if retry {
            return self
                .remove_retrying(py, timeout, |store, snapshot, deadline| store.expire_until(snapshot, now, deadline));
        }
        crate::detach(py, || self.handle.run(timeout, |timeout| self.handle.store.expire(timeout, now)))
            .map_err(to_py_err)
    }

    /// Removes every entry stored before the call. Returns how many it removed. `timeout` as for
    /// `expire`.
    #[pyo3(signature = (*, timeout = None, retry = false))]
    fn clear(&self, py: Python<'_>, timeout: Option<&Bound<'_, PyAny>>, retry: bool) -> PyResult<u64> {
        let timeout = self.config.call_timeout(timeout)?;
        if retry {
            return self.remove_retrying(py, timeout, Store::clear_until);
        }
        crate::detach(py, || self.handle.run(timeout, |timeout| self.handle.store.clear(timeout))).map_err(to_py_err)
    }

    /// Removes every entry tagged `tag` and stored before the call. Returns how many it removed.
    /// `evict(None)` removes nothing: untagged entries have no tag to evict by. `timeout` as for
    /// `expire`.
    #[pyo3(signature = (tag, *, timeout = None, retry = false))]
    fn evict(
        &self,
        py: Python<'_>,
        tag: &Bound<'_, PyAny>,
        timeout: Option<&Bound<'_, PyAny>>,
        retry: bool,
    ) -> PyResult<u64> {
        let timeout = self.config.call_timeout(timeout)?;
        let Some(encoded) = codec::key::encode_tag(Some(tag))? else { return Ok(0) };
        let tag = encoded.key();
        if retry {
            return self
                .remove_retrying(py, timeout, |store, snapshot, deadline| store.evict_until(snapshot, tag, deadline));
        }
        crate::detach(py, || self.handle.run(timeout, |timeout| self.handle.store.evict(timeout, tag)))
            .map_err(to_py_err)
    }

    /// Removes expired entries, then evicts until the cache is under `size_limit`. Returns how many
    /// it removed. `timeout` as for `expire`.
    #[pyo3(signature = (*, timeout = None, retry = false))]
    fn cull(&self, py: Python<'_>, timeout: Option<&Bound<'_, PyAny>>, retry: bool) -> PyResult<u64> {
        let timeout = self.config.call_timeout(timeout)?;
        let now = self.config.now(py)?;
        if retry {
            return self
                .remove_retrying(py, timeout, |store, snapshot, deadline| store.cull_until(snapshot, now, deadline));
        }
        crate::detach(py, || self.handle.run(timeout, |timeout| self.handle.store.cull(timeout, now)))
            .map_err(to_py_err)
    }

    /// Keys in sorted order (code-point order, like `sorted()`), optionally only those starting
    /// with `prefix`. Pages are fetched lazily, so deleting keys while iterating is safe.
    #[pyo3(signature = (*, prefix = None, reverse = false))]
    fn iterkeys(slf: Py<Self>, prefix: Option<String>, reverse: bool) -> KeyIterator {
        KeyIterator::new(slf, Order::Key { reverse }, prefix)
    }

    /// Keys in insertion order (an overwrite keeps its place).
    fn __iter__(slf: Py<Self>) -> KeyIterator {
        KeyIterator::new(slf, Order::Inserted, None)
    }

    /// Keys in insertion order backwards, newest first.
    fn __reversed__(slf: Py<Self>) -> KeyIterator {
        KeyIterator::new(slf, Order::InsertedReverse, None)
    }

    // -- lifecycle ----------------------------------------------------------------------------

    /// Releases this handle. When the last handle on a directory closes, every connection closes
    /// (D-018), and the store sweeps its empty value directories (D-042, `Store::release_handle`).
    /// Using the cache again afterwards reopens it, as diskcache does. Closing twice is harmless.
    ///
    /// With `checkpoint` (the default), the process's last handle first checkpoints a WAL left
    /// larger than its trim size (half of `wal_size_limit`), waiting at most 1 s (or `timeout`, if
    /// shorter) for other processes; a copy already under way can run past that (D-024). A small
    /// WAL, the usual case, costs one `stat`. Close never fails because of the checkpoint.
    ///
    /// Behind a pending fork it waits at most 1 s for that checkpoint and 1 s for the release, then
    /// returns and leaves the release for the end of the fork (D-027).
    #[pyo3(signature = (*, checkpoint = true))]
    fn close(&self, py: Python<'_>, checkpoint: bool) {
        // Before the swap: a logging handler that uses this cache would reopen a handle already marked
        // closed, and this close would then return with it open.
        if let Some(failures) = &self.config.failures {
            failures.flush(py, &self.config.directory);
        }
        if !self.handle.closed.swap(true, Ordering::AcqRel) {
            // The interpreter is released only when the last handle closes on a large WAL, so the
            // usual close doesn't wait to get it back. Then both steps run detached: the
            // checkpoint, and the release, since closing the last connection may run SQLite's own
            // checkpoint (with `checkpoint=False`, or after a busy one).
            if self.handle.store.closing_on_a_large_wal() {
                let budget = self.config.timeout.min(Duration::from_secs(1));
                crate::detach(py, || {
                    if checkpoint {
                        self.handle.store.checkpoint_on_close(budget);
                    }
                    self.handle.store.release_handle();
                });
            } else {
                self.handle.store.release_handle();
            }
        }
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[pyo3(signature = (exc_type, exc, tb, /))]
    fn __exit__(&self, py: Python<'_>, exc_type: &Bound<'_, PyAny>, exc: &Bound<'_, PyAny>, tb: &Bound<'_, PyAny>) {
        let _ = (exc_type, exc, tb);
        self.close(py, true);
    }

    /// A context manager holding a write transaction: `with cache.transact() as tx:`. On the
    /// thread that enters it, every call on this store (through any handle) inside the block runs
    /// in the transaction, which commits when the block exits and rolls back if it raises. Entered
    /// on a thread that already holds one on this store, it passes through: the inner exit
    /// doesn't commit. `retry=True` retries a `Timeout` (never `TransactionBusy`) until it begins.
    /// `retry` is a strict `bool`, as `expire` and `timeout` refuse one (D-014): `retry=1` raises
    /// `TypeError`.
    #[pyo3(signature = (retry = false))]
    fn transact(slf: Py<Self>, retry: bool) -> Transaction {
        Transaction::new(slf, retry)
    }
}

/// `eviction_policy=` by diskcache's name. The two policies that need a write on every read are
/// refused, saying so (plan 24, decision 6).
pub(crate) fn parse_policy(name: &str) -> PyResult<EvictionPolicy> {
    match name {
        "least-recently-stored" => Ok(EvictionPolicy::LeastRecentlyStored),
        "none" => Ok(EvictionPolicy::None),
        "least-recently-used" | "least-frequently-used" => Err(PyValueError::new_err(format!(
            "eviction_policy={name:?} isn't supported: it makes every read a write, which would serialize reads \
             across processes (D-022); use 'least-recently-stored' or 'none'"
        ))),
        other => Err(PyValueError::new_err(format!(
            "eviction_policy must be 'least-recently-stored' or 'none', not {other:?}"
        ))),
    }
}

/// The constructor arguments `Cache` and `FanoutCache` share, validated in the order `Cache`
/// always checked them.
struct Settings {
    timeout: Duration,
    requested: Requested,
    /// `None` when not given: a `Cache` takes the default, a `FanoutCache` two per shard.
    max_connections: Option<u64>,
    cache_pages: u64,
    wal_size_limit: u64,
    auto_checkpoint: bool,
    serializer: Serializer,
}

impl Settings {
    #[allow(clippy::too_many_arguments)]
    fn parse(
        py: Python<'_>,
        timeout: Option<&Bound<'_, PyAny>>,
        size_limit: Option<&Bound<'_, PyAny>>,
        cull_limit: Option<&Bound<'_, PyAny>>,
        eviction_policy: Option<&str>,
        serializer: &str,
        max_connections: Option<&Bound<'_, PyAny>>,
        sqlite_cache_size: Option<&Bound<'_, PyAny>>,
        wal_size_limit: Option<&Bound<'_, PyAny>>,
        wal_checkpoint: &str,
    ) -> PyResult<Self> {
        codec::load_types(py)?;
        let timeout = timeout_duration(match timeout {
            Some(t) => seconds("timeout", t)?,
            None => 60.0,
        })?;
        let requested = Requested {
            size_limit: whole("size_limit", size_limit)?,
            cull_limit: whole("cull_limit", cull_limit)?,
            eviction_policy: eviction_policy.map(parse_policy).transpose()?,
        };
        let max_connections = match whole("max_connections", max_connections)? {
            Some(n) if n < 1 => return Err(PyValueError::new_err("max_connections must be at least 1")),
            Some(n) => Some(n.unsigned_abs()),
            None => None,
        };
        let cache_pages = match whole("sqlite_cache_size", sqlite_cache_size)? {
            Some(n) if n < 1 => return Err(PyValueError::new_err("sqlite_cache_size must be at least 1 page")),
            // SQLite reads `PRAGMA cache_size` as a 32-bit int; larger values would wrap.
            Some(n) if n > i64::from(i32::MAX) => {
                return Err(PyValueError::new_err(format!("sqlite_cache_size must be at most {} pages", i32::MAX)));
            }
            Some(n) => n.unsigned_abs(),
            None => DEFAULT_CACHE_PAGES,
        };
        let min_wal = diskcache_oxide_core::wal::MIN_SIZE_LIMIT;
        let wal_size_limit = match whole("wal_size_limit", wal_size_limit)? {
            Some(n) if n < 0 || n.unsigned_abs() < min_wal => {
                return Err(PyValueError::new_err(format!("wal_size_limit must be at least {min_wal} bytes")));
            }
            Some(n) => n.unsigned_abs(),
            None => DEFAULT_WAL_SIZE_LIMIT,
        };
        let auto_checkpoint = match wal_checkpoint {
            "manual" => false,
            "auto" => true,
            other => {
                return Err(PyValueError::new_err(format!("wal_checkpoint must be 'manual' or 'auto', got {other:?}")));
            }
        };
        let serializer = Serializer::from_name(serializer)?;
        Ok(Self { timeout, requested, max_connections, cache_pages, wal_size_limit, auto_checkpoint, serializer })
    }

    /// The store options for `directory`, with `max_connections` resolved by the caller.
    fn options(&self, directory: PathBuf, max_connections: u64, role: Role) -> OpenOptions {
        OpenOptions {
            directory,
            timeout: self.timeout,
            requested: self.requested,
            max_connections,
            cache_pages: self.cache_pages,
            wal_size_limit: self.wal_size_limit,
            auto_checkpoint: self.auto_checkpoint,
            role,
            default_size_limit: DEFAULT_SIZE_LIMIT,
        }
    }
}

impl StoreHandle {
    /// A handle that was closed and is used again rejoins its store (diskcache reopens lazily).
    /// Returns what's left of `timeout` for the operation: all of it, unless the handle was closed.
    #[inline]
    fn ensure_open(&self, timeout: Duration) -> Result<Duration, Error> {
        if self.closed.load(Ordering::Acquire) { self.reopen(timeout) } else { Ok(timeout) }
    }

    /// Runs `call` with what's left of `timeout` once the handle is open: `ensure_open` then `call`,
    /// except that a `Timeout` from `call` on a handle that had to rejoin names `timeout`, the call's
    /// whole budget, not the part left over. The default; `ensure_open` is for callers that read a
    /// `Timeout` as an answer or rename it themselves.
    fn run<T>(&self, timeout: Duration, call: impl FnOnce(Duration) -> Result<T, Error>) -> Result<T, Error> {
        if !self.closed.load(Ordering::Acquire) {
            return call(timeout);
        }
        call(self.reopen(timeout)?).map_err(|error| match error {
            Error::Timeout { .. } => Error::Timeout { seconds: timeout.as_secs_f64() },
            other => other,
        })
    }

    /// The closed flag flips under the gate level the count is taken on. A timeout before that
    /// changes nothing: the handle stays closed and isn't counted. Flipping it first would leave the
    /// count one too low once another thread's `close` released it.
    #[cold]
    fn reopen(&self, timeout: Duration) -> Result<Duration, Error> {
        let deadline = lockwait::deadline_after(Instant::now(), timeout);
        self.store.add_handle_until(deadline, || self.closed.swap(false, Ordering::AcqRel)).map_err(
            |error| match error {
                Error::Timeout { .. } => Error::Timeout { seconds: timeout.as_secs_f64() },
                other => other,
            },
        )?;
        Ok(deadline.saturating_duration_since(Instant::now()))
    }
}

thread_local! {
    /// True while a `retry=True` call runs on this thread: a durable handle then raises a `Timeout`
    /// for the call to retry, instead of answering it as a miss. Every call sets it for its own
    /// span, so a call nested inside one (a pickle hook using the cache) has its own setting.
    static RETRYING: Cell<bool> = const { Cell::new(false) };

    /// Set when this thread's current call raises its own store's `Timeout` (in `store_failed`), so
    /// `retrying` retries only that: a `Timeout` raised by the caller's code (a pickle or registered
    /// type's hook, a `clock`, a nested cache call inside one) is never taken for this cache's.
    static OWN_TIMEOUT: Cell<bool> = const { Cell::new(false) };
}

/// The least a round of a `retry=True` call takes, sleeping out the rest with the interpreter
/// released: a `timeout=0` call would otherwise spin.
const RETRY_ROUND: Duration = Duration::from_millis(1);

/// Runs `call`, and with `retry` again after every `Timeout` until it doesn't time out (diskcache's
/// `retry=True`). Nothing else is retried: not `TransactionBusy`, a poisoned child's `RuntimeError`
/// or a decode error. Each round gets the call's whole `timeout` and holds nothing between rounds,
/// so a pending fork is never held up by the wait; signals are checked, so Ctrl-C ends it.
pub(crate) fn retrying<T>(py: Python<'_>, retry: bool, mut call: impl FnMut() -> PyResult<T>) -> PyResult<T> {
    struct Restore {
        retrying: bool,
        own: bool,
    }
    impl Drop for Restore {
        fn drop(&mut self) {
            RETRYING.with(|r| r.set(self.retrying));
            OWN_TIMEOUT.with(|o| o.set(self.own));
        }
    }
    let _restore =
        Restore { retrying: RETRYING.with(|r| r.replace(retry)), own: OWN_TIMEOUT.with(|o| o.replace(false)) };
    loop {
        // A round's clock is read only when it may be retried.
        let started = retry.then(Instant::now);
        OWN_TIMEOUT.with(|o| o.set(false));
        match call() {
            Err(e) if retry && OWN_TIMEOUT.with(Cell::get) && e.is_instance_of::<crate::Timeout>(py) => {
                pause_before_retry(py, started.unwrap_or_else(Instant::now))?;
            }
            other => return other,
        }
    }
}

/// Between two rounds of a retried call: signals first, then the rest of [`RETRY_ROUND`], detached.
pub(crate) fn pause_before_retry(py: Python<'_>, started: Instant) -> PyResult<()> {
    py.check_signals()?;
    let rest = RETRY_ROUND.saturating_sub(started.elapsed());
    if !rest.is_zero() {
        crate::detach(py, || std::thread::sleep(rest));
    }
    Ok(())
}

impl HandleConfig {
    #[allow(clippy::too_many_arguments)]
    fn new(
        directory: &std::path::Path,
        settings: &Settings,
        pickle: bool,
        types: Option<Py<TypeRegistry>>,
        clock: Option<Py<PyAny>>,
        on_decode_error: Option<DecodePolicy>,
        durable: bool,
        reopen: Py<pyo3::types::PyDict>,
    ) -> Self {
        let timeout = settings.timeout;
        Self {
            directory: directory.to_string_lossy().into_owned(),
            timeout,
            codec: Codec { serializer: settings.serializer, pickle, types },
            clock,
            on_decode_error: on_decode_error.unwrap_or(if durable { DecodePolicy::Miss } else { DecodePolicy::Raise }),
            failures: durable.then(|| Box::new(Failures::new(timeout.as_secs_f64()))),
            reported: Mutex::new(HashSet::new()),
            reopen,
        }
    }

    /// Visits the Python objects the handle holds, for `__traverse__`.
    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.codec.traverse(visit)?;
        visit.call(&self.reopen)?;
        if let Some(clock) = &self.clock {
            visit.call(clock)?;
        }
        if let DecodePolicy::Report(callback) = &self.on_decode_error {
            visit.call(callback)?;
        }
        Ok(())
    }

    fn now(&self, py: Python<'_>) -> PyResult<f64> {
        match &self.clock {
            Some(clock) => clock.bind(py).call0()?.extract::<f64>(),
            None => Ok(SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())),
        }
    }

    /// A per-call `timeout=` argument, or the cache's own when it's omitted or None.
    fn call_timeout(&self, timeout: Option<&Bound<'_, PyAny>>) -> PyResult<Duration> {
        match optional_seconds("timeout", timeout)? {
            Some(t) => timeout_duration(t),
            None => Ok(self.timeout),
        }
    }

    /// The result of a call on `store`, or `None` if it failed and this handle answers that as a
    /// miss. The one place a failure can be swallowed: only a store's own failure ever gets here, so
    /// a `Timeout` raised by the caller's code (a clock, a pickle hook) is never taken for this
    /// cache's.
    #[inline]
    fn attempt<T>(
        &self,
        py: Python<'_>,
        store: &Store,
        op: Op,
        now: f64,
        result: Result<T, Error>,
    ) -> PyResult<Option<T>> {
        match result {
            Ok(value) => Ok(Some(value)),
            Err(error) => self.store_failed(py, store, op, now, error).map(|()| None),
        }
    }

    /// `Ok` if `error` was swallowed and counted, or the exception to raise: it isn't durable, it's a
    /// bug rather than an outage, or this thread holds a transaction on `store`, whose atomicity a
    /// dropped write would break.
    #[cold]
    #[inline(never)]
    fn store_failed(&self, py: Python<'_>, store: &Store, op: Op, now: f64, error: Error) -> PyResult<()> {
        if matches!(error, Error::Timeout { .. }) {
            OWN_TIMEOUT.with(|o| o.set(true));
        }
        let Some(failures) = self.failures.as_deref() else { return Err(to_py_err(error)) };
        // Under `retry=True` a `Timeout` is the call's to retry, not an outage to answer (plan 24).
        let retried = matches!(error, Error::Timeout { .. }) && RETRYING.with(Cell::get);
        if !error.is_operational() || store.held_by_this_thread() || retried {
            return Err(to_py_err(error));
        }
        failures.record(py, op, now, &error, &self.directory)
    }

    // -- the routed operations: one key, on the store `handle` names ------------------------------
    //
    // `Cache` passes its one store; `FanoutCache` the shard the key hashes to. The key is already
    // validated (D-004): everything after that is here, so both behave exactly alike.

    fn get(
        &self,
        py: Python<'_>,
        handle: &StoreHandle,
        key: Key<'_>,
        default: Option<Py<PyAny>>,
        wants: Wants,
    ) -> PyResult<Py<PyAny>> {
        let now = self.now(py)?;
        match self.lookup(py, handle, key, now, wants.tag)? {
            Some((value, expires, tag)) => wants.answer(py, value, expires, tag.as_ref()),
            None => wants.answer(py, default.unwrap_or_else(|| py.None()), None, None),
        }
    }

    /// `cache[key]`: `name` is `key` validated; a miss raises `KeyError(key)`.
    fn get_item(
        &self,
        py: Python<'_>,
        handle: &StoreHandle,
        key: &Bound<'_, PyAny>,
        name: Key<'_>,
    ) -> PyResult<Py<PyAny>> {
        let now = self.now(py)?;
        match self.lookup(py, handle, name, now, false)? {
            Some((value, ..)) => Ok(value),
            None => Err(PyKeyError::new_err(key.clone().unbind())),
        }
    }

    fn contains(&self, py: Python<'_>, handle: &StoreHandle, key: Key<'_>) -> PyResult<bool> {
        let now = self.now(py)?;
        let found = handle.run(self.timeout, |timeout| handle.store.contains(timeout, key, now));
        Ok(self.attempt(py, &handle.store, Op::Contains, now, found)?.unwrap_or(false))
    }

    /// `set` and `add`'s value and `expire`, checked and encoded.
    fn encode_value<'py>(
        &self,
        py: Python<'py>,
        value: &Bound<'py, PyAny>,
        expire: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<(codec::Encoded<'py>, Option<f64>)> {
        let expire = optional_seconds("expire", expire)?;
        Ok((self.codec.encode(py, value)?, expire))
    }

    /// `value` was encoded by [`HandleConfig::encode_value`], once, outside any retry: a retried
    /// round re-runs only the store's work, not the encoder or a registered type's hooks.
    fn set(
        &self,
        py: Python<'_>,
        handle: &StoreHandle,
        key: Key<'_>,
        encoded: &codec::Encoded<'_>,
        expire: Option<f64>,
        tag: Option<Tag<'_>>,
    ) -> PyResult<bool> {
        let value = stored_value(&encoded.payload, encoded.mode.as_u8())?;
        let now = self.now(py)?;
        let stored = handle.run(self.timeout, |timeout| handle.store.set_tagged(timeout, key, value, expire, tag, now));
        Ok(self.attempt(py, &handle.store, Op::Set, now, stored)?.unwrap_or(false))
    }

    /// `value` was encoded by [`HandleConfig::encode_value`], once, outside any retry: a retried
    /// round re-runs only the store's work, not the encoder or a registered type's hooks.
    fn add(
        &self,
        py: Python<'_>,
        handle: &StoreHandle,
        key: Key<'_>,
        encoded: &codec::Encoded<'_>,
        expire: Option<f64>,
        tag: Option<Tag<'_>>,
    ) -> PyResult<bool> {
        let value = stored_value(&encoded.payload, encoded.mode.as_u8())?;
        let now = self.now(py)?;
        let added = handle.run(self.timeout, |timeout| handle.store.add_tagged(timeout, key, value, expire, tag, now));
        Ok(self.attempt(py, &handle.store, Op::Add, now, added)?.unwrap_or(false))
    }

    fn delete(&self, py: Python<'_>, handle: &StoreHandle, key: Key<'_>) -> PyResult<bool> {
        let now = self.now(py)?;
        let deleted = handle.run(self.timeout, |timeout| handle.store.delete(timeout, key, now));
        Ok(self.attempt(py, &handle.store, Op::Delete, now, deleted)?.unwrap_or(false))
    }

    fn pop(
        &self,
        py: Python<'_>,
        handle: &StoreHandle,
        key: Key<'_>,
        default: Option<Py<PyAny>>,
        wants: Wants,
    ) -> PyResult<Py<PyAny>> {
        let now = self.now(py)?;
        // In a child that can't write (D-020) a read could report a miss; a pop fails.
        let ready = handle.run(self.timeout, |timeout| handle.store.check_writable().map(|()| timeout));
        let popped = match self.attempt(py, &handle.store, Op::Pop, now, ready)? {
            Some(timeout) => self.pop_value(py, handle, key, now, timeout, wants.tag)?,
            None => None,
        };
        match popped {
            Some((value, expires, tag)) => wants.answer(py, value, expires, tag.as_ref()),
            None => wants.answer(py, default.unwrap_or_else(|| py.None()), None, None),
        }
    }

    fn touch(
        &self,
        py: Python<'_>,
        handle: &StoreHandle,
        key: Key<'_>,
        expire: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let expire = optional_seconds("expire", expire)?;
        let now = self.now(py)?;
        let touched = handle.run(self.timeout, |timeout| handle.store.touch(timeout, key, expire, now));
        Ok(self.attempt(py, &handle.store, Op::Touch, now, touched)?.unwrap_or(false))
    }

    fn lookup(
        &self,
        py: Python<'_>,
        handle: &StoreHandle,
        key: Key<'_>,
        now: f64,
        with_tag: bool,
    ) -> PyResult<Option<Found>> {
        let store = &*handle.store;
        let read = handle.run(self.timeout, |timeout| self.read_row(py, store, timeout, key, now, with_tag));
        // `stats()`: a durable outage answered as a miss counts as nothing, as diskcache's `Timeout`.
        let hit = match self.attempt(py, store, Op::Get, now, read)? {
            Some(Some(hit)) => hit,
            Some(None) => {
                store.stats().record(false);
                return Ok(None);
            }
            None => return Ok(None),
        };
        match hit.value {
            Ok(value) => {
                store.stats().record(true);
                Ok(Some((value, hit.expire_time, hit.tag)))
            }
            Err((mode, e)) => {
                // A value that can't be decoded and is answered as a miss counts as one.
                self.decode_failed(py, store, handle.shard, key, Op::Get, mode, e, Row::Stored(hit.version))?;
                store.stats().record(false);
                Ok(None)
            }
        }
    }

    /// The live value of `key` in `store`, decoded, or the decode error with the row's mode. A store
    /// failure is the outer `Err`, so the decode policy never sees one.
    #[inline]
    fn read_row(
        &self,
        py: Python<'_>,
        store: &Store,
        timeout: Duration,
        key: Key<'_>,
        now: f64,
        with_tag: bool,
    ) -> Result<Option<Decoded>, Error> {
        let codec = &self.codec;
        store.get_tagged(
            timeout,
            key,
            now,
            with_tag,
            |mode| codec.refuses(mode),
            |mode, value| Ok(decode_value(py, codec, mode, value).map_err(|e| (mode, e))),
        )
    }

    /// `pop`, with a miss as `None`. Reads and decodes as `get` does, with no write lock and with
    /// pickle hooks running where a `get`'s run, then deletes exactly that version, so a value it
    /// can't read stays. A caller that loses the delete to another writer reads again, and after
    /// `OPTIMISTIC_ATTEMPTS` of those takes the locked `Store::pop`, which can't starve.
    ///
    /// The whole call shares one deadline and the caller's one clock reading, so losing races and
    /// slow hooks can't stretch it to many times the cache's timeout. A race lost after that deadline
    /// raises `Timeout` and deletes nothing: the fallback removes before it decodes, so it's only for
    /// a caller that really lost `OPTIMISTIC_ATTEMPTS` times, not one that ran out of time.
    ///
    /// On a durable handle a store failure is a miss. That includes a failed delete after a clean
    /// decode: the row is still there, so returning the value could deliver it twice.
    fn pop_value(
        &self,
        py: Python<'_>,
        handle: &StoreHandle,
        key: Key<'_>,
        now: f64,
        timeout: Duration,
        with_tag: bool,
    ) -> PyResult<Option<Found>> {
        let (store, shard) = (&*handle.store, handle.shard);
        let deadline = lockwait::deadline_after(Instant::now(), timeout);
        let left = || deadline.saturating_duration_since(Instant::now());
        for _ in 0..spill::OPTIMISTIC_ATTEMPTS {
            let read = self.read_row(py, store, left(), key, now, with_tag);
            let Some(Some(hit)) = self.pop_attempt(py, store, now, read)? else { return Ok(None) };
            match hit.value {
                Ok(value) => {
                    let deleted = store.delete_version(left(), hit.version);
                    match self.pop_attempt(py, store, now, deleted)? {
                        Some(true) => return Ok(Some((value, hit.expire_time, hit.tag))),
                        Some(false) => {}
                        None => return Ok(None),
                    }
                }
                Err((mode, e)) => {
                    if self.pop_failed(py, store, shard, key, now, mode, e, hit.version, left())? {
                        return Ok(None);
                    }
                }
            }
            if left().is_zero() {
                self.pop_attempt(py, store, now, Err::<(), _>(Error::Timeout { seconds: 0.0 }))?;
                return Ok(None);
            }
        }
        let codec = &self.codec;
        let refuse = |mode| codec.refuses(mode);
        let removed = store.pop_tagged(left(), key, now, with_tag, refuse);
        match self.pop_attempt(py, store, now, removed)? {
            None | Some(Pop::Missing) => Ok(None),
            Some(Pop::Removed(popped)) => match decode_value(py, codec, popped.mode, popped.value_ref()) {
                Ok(value) => Ok(Some((value, popped.expire_time, popped.tag))),
                Err(e) => {
                    self.decode_failed(py, store, shard, key, Op::Pop, popped.mode, e, Row::Removed).map(|()| None)
                }
            },
            Some(Pop::Refused { mode }) => {
                let e = codec::refusal(mode);
                self.decode_failed(py, store, shard, key, Op::Pop, mode, e, Row::Kept).map(|()| None)
            }
        }
    }

    /// [`HandleConfig::attempt`] for a store call during `pop`.
    fn pop_attempt<T>(&self, py: Python<'_>, store: &Store, now: f64, result: Result<T, Error>) -> PyResult<Option<T>> {
        self.attempt(py, store, Op::Pop, now, result.map_err(|e| self.whole_timeout(e)))
    }

    /// A store failure during `pop`. A timeout names the cache's timeout: the store only knows the
    /// part of it that was left.
    fn whole_timeout(&self, error: Error) -> Error {
        match error {
            Error::Timeout { .. } => Error::Timeout { seconds: self.timeout.as_secs_f64() },
            other => other,
        }
    }

    /// The value `pop` read as `version` failed to decode with `e`. Returns whether the `pop` is over
    /// as a miss; `false` means another writer replaced or removed that version first, so read again.
    ///
    /// An unreadable value goes under every policy, waiting for the write lock as any `pop` does,
    /// before the error is raised or reported. If that delete fails, its error is raised with `e` as
    /// its cause, unless this handle answers it as a miss: then the row stays and `e` goes through the
    /// decode policy. A foreign value, or an error that isn't about the stored bytes, keeps the row.
    #[allow(clippy::too_many_arguments)]
    fn pop_failed(
        &self,
        py: Python<'_>,
        store: &Store,
        shard: usize,
        key: Key<'_>,
        now: f64,
        mode: i64,
        e: PyErr,
        version: Version,
        timeout: Duration,
    ) -> PyResult<bool> {
        if !matches!(self.classify(py, store, mode, &e), Failure::Unreadable) {
            return self.decode_failed(py, store, shard, key, Op::Pop, mode, e, Row::Stored(version)).map(|()| true);
        }
        match store.delete_version(timeout, version) {
            Ok(true) => self.decode_failed(py, store, shard, key, Op::Pop, mode, e, Row::Removed).map(|()| true),
            Ok(false) => Ok(false),
            Err(error) => match self.store_failed(py, store, Op::Pop, now, self.whole_timeout(error)) {
                Ok(()) => {
                    self.decode_failed(py, store, shard, key, Op::Pop, mode, e, Row::Stored(version)).map(|()| true)
                }
                Err(error) => {
                    error.set_cause(py, Some(e));
                    Err(error)
                }
            },
        }
    }

    /// A value of `key` in `store` failed to decode with `e`. Under `on_decode_error='raise'`, or for
    /// an error that isn't about the stored bytes, returns `e`. Otherwise the read is a miss: an
    /// unreadable row is deleted (exactly that version, without waiting), a foreign one is left for
    /// whoever can read it, and the failure is reported, once per stored version for a row that
    /// stays. Runs with no connection of its own held; inside a pickle hook of an outer read, that
    /// read's connection is still held, so a slow report delays forks and checkpoints for as long.
    #[allow(clippy::too_many_arguments)]
    fn decode_failed(
        &self,
        py: Python<'_>,
        store: &Store,
        shard: usize,
        key: Key<'_>,
        op: Op,
        mode: i64,
        e: PyErr,
        row: Row,
    ) -> PyResult<()> {
        if matches!(self.on_decode_error, DecodePolicy::Raise) {
            return Err(e);
        }
        let failure = self.classify(py, store, mode, &e);
        if matches!(failure, Failure::Transient)
            && let Some(failures) = &self.failures
        {
            failures.count_hook_outage(op, e.is_instance_of::<PyTimeoutError>(py));
        }
        let (outcome, stays) = match (failure, row) {
            (Failure::Passthrough, _) => return Err(e),
            (Failure::Transient, Row::Stored(version)) => {
                ("left in place, since the error may not last", Some(version))
            }
            (Failure::Transient, Row::Kept) => ("left in place, since the error may not last", None),
            (Failure::Unreadable, Row::Removed) => ("deleted", None),
            (Failure::Foreign | Failure::Transient, Row::Removed) => {
                ("deleted, since pop lost too many races and had to remove it first", None)
            }
            (Failure::Foreign | Failure::Unreadable, Row::Kept) => ("left in place", None),
            (Failure::Foreign, Row::Stored(version)) => {
                ("left in place for a version or host that can read it", Some(version))
            }
            (Failure::Unreadable, Row::Stored(version)) => match store.delete_version_nowait(version) {
                VersionDelete::Removed => ("deleted", None),
                VersionDelete::Gone => ("already replaced", None),
                VersionDelete::Skipped => (
                    "not deleted now (it would have had to wait, or a transaction is open); a later read retries",
                    Some(version),
                ),
            },
        };
        // A row that stays fails again on every read: a newer peer's values during a deploy can
        // be read thousands of times a second. Report each stored version once.
        // Inside another report nothing is reported (see `report_decode_error`), so don't mark the
        // version as reported either: a later top-level read must still report it.
        if REPORTING.get() {
            return Ok(());
        }
        if let Some(version) = stays
            && !self.first_report(shard, version)
        {
            return Ok(());
        }
        self.report_decode_error(py, key, e, outcome);
        Ok(())
    }

    /// What kind of decode failure `e` is on this handle. A durable handle takes a pickle hook's
    /// `OSError` (`Timeout` included) or `RecursionError` as an outage: a miss with the row kept, so
    /// the request doesn't fail. Not inside this thread's own transaction on `store`, which raises as
    /// any failure there does.
    fn classify(&self, py: Python<'_>, store: &Store, mode: i64, e: &PyErr) -> Failure {
        match classify(py, mode, e) {
            Failure::Passthrough
                if self.failures.is_some()
                    && (e.is_instance_of::<PyOSError>(py) || e.is_instance_of::<PyRecursionError>(py))
                    && !store.held_by_this_thread() =>
            {
                Failure::Transient
            }
            failure => failure,
        }
    }

    /// True the first time `version` of shard `shard` is reported by this handle (remembering at
    /// most `REPORTED_VERSIONS`, then starting over).
    fn first_report(&self, shard: usize, version: Version) -> bool {
        let mut reported = self.reported.lock().unwrap_or_else(PoisonError::into_inner);
        if reported.len() >= REPORTED_VERSIONS {
            reported.clear();
        }
        reported.insert((shard, version))
    }

    /// Logs a decode failure, or hands it to the `on_decode_error` callable. Never raises: a report
    /// that fails is logged. A decode failure inside a report (the callable, or a logging handler,
    /// reading the same bad key) isn't reported again, so a report can never recurse.
    fn report_decode_error(&self, py: Python<'_>, key: Key<'_>, e: PyErr, outcome: &str) {
        let Some(_reporting) = Reporting::enter() else { return };
        let logged =
            codec::key::decode_key(py, key).and_then(|key| logger(py).map(|log| (log, key))).and_then(|(log, key)| {
                let kwargs = pyo3::types::PyDict::new(py);
                if let DecodePolicy::Report(callback) = &self.on_decode_error {
                    let Err(failed) = callback.bind(py).call1((key.clone_ref(py), e.into_value(py))) else {
                        return Ok(());
                    };
                    kwargs.set_item("exc_info", failed.into_value(py))?;
                    let message = ("on_decode_error callback failed for %r in %s", key, &self.directory);
                    log.call_method("error", message, Some(&kwargs)).map(drop)
                } else {
                    kwargs.set_item("exc_info", e.value(py))?;
                    let message = (
                        "could not decode the value of %r in %s, so it was a miss (%s): %r",
                        key,
                        &self.directory,
                        outcome,
                        e.value(py),
                    );
                    log.call_method("warning", message, Some(&kwargs)).map(drop)
                }
            });
        if let Err(lost) = logged {
            lost.write_unraisable(py, None);
        }
    }

    /// `incr`/`decr`: `name` is `key` validated; a missing key with no default raises
    /// `KeyError(key)`.
    #[allow(clippy::too_many_arguments)]
    fn add_number(
        &self,
        py: Python<'_>,
        handle: &StoreHandle,
        key: &Bound<'_, PyAny>,
        name: Key<'_>,
        Delta(mut delta): Delta,
        IncrDefault(default): IncrDefault,
        negate: bool,
    ) -> PyResult<Py<PyAny>> {
        if negate {
            delta = match delta {
                Number::Int(n) => {
                    Number::Int(n.checked_neg().ok_or_else(|| PyOverflowError::new_err("integer overflow"))?)
                }
                Number::Real(x) => Number::Real(-x),
            };
        }
        let now = self.now(py)?;
        let op = if negate { Op::Decr } else { Op::Incr };
        let store = &*handle.store;
        let stored = handle.run(self.timeout, |timeout| store.incr(timeout, name, delta, default, now));
        let number = match stored {
            Ok(number) => number,
            Err(Error::KeyMissing) => return Err(PyKeyError::new_err(key.clone().unbind())),
            Err(error) => {
                // Answered as a `NullCache` does, so a rate limiter fails open.
                self.store_failed(py, store, op, now, error)?;
                let Some(default) = default else { return Err(PyKeyError::new_err(key.clone().unbind())) };
                default.checked_add(delta).map_err(to_py_err)?
            }
        };
        match number {
            Number::Int(n) => Ok(n.into_pyobject(py)?.into_any().unbind()),
            Number::Real(x) => Ok(PyFloat::new(py, x).into_any().unbind()),
        }
    }
}

impl Drop for Cache {
    /// A cache dropped without `close()`: releases its handle, detached on a large WAL as in
    /// `close()`. Drop runs from the object's deallocation, attached, so `try_attach` always
    /// succeeds, during interpreter shutdown too, where detaching is safe (the release is done
    /// before the thread asks for the interpreter back). Releasing attached when it declines is
    /// only a fallback.
    fn drop(&mut self) {
        if !self.handle.closed.swap(true, Ordering::AcqRel) {
            let store = &self.handle.store;
            let detached = store.closing_on_a_large_wal()
                && Python::try_attach(|py| crate::detach(py, || store.release_handle())).is_some();
            if !detached {
                store.release_handle();
            }
        }
    }
}

impl Cache {
    /// `expire`, `clear` or `cull` with `retry=True`: one snapshot, so the call removes only what was
    /// stored before it started, then rounds of `remove` from it, each with the whole `timeout`,
    /// until one finishes. Returns what every round removed.
    fn remove_retrying(
        &self,
        py: Python<'_>,
        timeout: Duration,
        remove: impl Fn(&Store, Snapshot, Instant) -> Result<u64, Interrupted> + Sync,
    ) -> PyResult<u64> {
        let handle = &self.handle;
        let snapshot = loop {
            let started = Instant::now();
            let taken = crate::detach(py, || {
                handle.run(timeout, |left| handle.store.snapshot_until(lockwait::deadline_after(Instant::now(), left)))
            });
            match taken {
                Ok(snapshot) => break snapshot,
                Err(Error::Timeout { .. }) => pause_before_retry(py, started)?,
                Err(error) => return Err(to_py_err(error)),
            }
        };
        let mut removed = 0;
        loop {
            let started = Instant::now();
            // A `close()` from another thread between rounds: rejoin, as every other call does.
            let left = match crate::detach(py, || handle.ensure_open(timeout)) {
                Ok(left) => left,
                Err(Error::Timeout { .. }) => {
                    pause_before_retry(py, started)?;
                    continue;
                }
                Err(error) => return Err(to_py_err(error)),
            };
            let deadline = lockwait::deadline_after(Instant::now(), left);
            match crate::detach(py, || remove(&handle.store, snapshot, deadline)) {
                Ok(n) => return Ok(removed + n),
                Err(Interrupted { removed: n, error: Error::Timeout { .. } }) => {
                    removed += n;
                    pause_before_retry(py, started)?;
                }
                Err(Interrupted { error, .. }) => return Err(to_py_err(error)),
            }
        }
    }
}

/// `Cache.memoize` and `FanoutCache.memoize`: `diskcache_oxide._memoize.memoize(cache, ...)`. Reads
/// and writes pass `retry=True`, as diskcache's do, except on a durable handle: there `retry` would
/// make an outage wait instead of running the function, which is what `durable=True` is for.
pub(crate) fn memoize_on(
    cache: &Bound<'_, PyAny>,
    name: Option<&Bound<'_, PyAny>>,
    typed: bool,
    expire: Option<&Bound<'_, PyAny>>,
    tag: Option<&Bound<'_, PyAny>>,
    ignore: Option<&Bound<'_, PyAny>>,
    durable: bool,
) -> PyResult<Py<PyAny>> {
    let py = cache.py();
    let ignore = match ignore {
        Some(ignore) => ignore.clone(),
        None => PyTuple::empty(py).into_any(),
    };
    let memoize = py.import("diskcache_oxide._memoize")?.getattr("memoize")?;
    memoize.call1((cache, name, typed, expire, tag, ignore, !durable)).map(Bound::unbind)
}

/// What `__reduce__` returns: `diskcache_oxide._pickling.rebuild` and its arguments.
pub(crate) type Reduced = (Py<PyAny>, (&'static str, Py<pyo3::types::PyDict>));

/// A handle's options as passed, for [`HandleConfig::reopen`]: only those given, so an unpickled
/// handle gets today's defaults for the rest.
pub(crate) struct ReopenOptions<'a, 'py> {
    pub timeout: Option<&'a Bound<'py, PyAny>>,
    pub serializer: &'a str,
    pub pickle: bool,
    pub types: Option<&'a Py<TypeRegistry>>,
    pub clock: Option<&'a Py<PyAny>>,
    pub max_connections: Option<&'a Bound<'py, PyAny>>,
    pub sqlite_cache_size: Option<&'a Bound<'py, PyAny>>,
    pub wal_size_limit: Option<&'a Bound<'py, PyAny>>,
    pub wal_checkpoint: &'a str,
    pub on_decode_error: Option<&'a DecodePolicy>,
    pub durable: bool,
}

impl ReopenOptions<'_, '_> {
    pub(crate) fn build(&self, py: Python<'_>, directory: &std::path::Path) -> PyResult<Py<pyo3::types::PyDict>> {
        let d = pyo3::types::PyDict::new(py);
        let absolute = std::path::absolute(directory).unwrap_or_else(|_| directory.to_path_buf());
        d.set_item("directory", absolute)?;
        if let Some(timeout) = self.timeout {
            d.set_item("timeout", timeout)?;
        }
        if self.serializer != "msgpack" {
            d.set_item("serializer", self.serializer)?;
        }
        if self.pickle {
            d.set_item("pickle", true)?;
        }
        if let Some(types) = self.types {
            d.set_item("types", types)?;
        }
        if let Some(clock) = self.clock {
            d.set_item("clock", clock)?;
        }
        for (name, value) in [
            ("max_connections", self.max_connections),
            ("sqlite_cache_size", self.sqlite_cache_size),
            ("wal_size_limit", self.wal_size_limit),
        ] {
            if let Some(value) = value {
                d.set_item(name, value)?;
            }
        }
        if self.wal_checkpoint != "manual" {
            d.set_item("wal_checkpoint", self.wal_checkpoint)?;
        }
        match self.on_decode_error {
            Some(DecodePolicy::Raise) => d.set_item("on_decode_error", "raise")?,
            Some(DecodePolicy::Miss) => d.set_item("on_decode_error", "miss")?,
            Some(DecodePolicy::Report(callback)) => d.set_item("on_decode_error", callback)?,
            None => {}
        }
        if self.durable {
            d.set_item("durable", true)?;
        }
        Ok(d.unbind())
    }
}

/// Which limit a `reset(key, ...)` names, and the value to set (`None` to read it).
pub(crate) enum ResetLimit {
    Size(Option<i64>),
    Cull(Option<i64>),
    Policy(Option<EvictionPolicy>),
}

impl ResetLimit {
    /// `reset(key, value=ENOVAL, update=True)`'s arguments, checked as the constructor checks them.
    pub(crate) fn parse(key: &str, value: Option<&Bound<'_, PyAny>>, update: bool) -> PyResult<Self> {
        let given = match value {
            Some(v) if !v.is(v.py().import("diskcache_oxide._constants")?.getattr("ENOVAL")?) => Some(v),
            _ => None,
        };
        let parsed = match key {
            "size_limit" => Self::Size(whole("size_limit", given)?),
            "cull_limit" => Self::Cull(whole("cull_limit", given)?),
            _ if key.starts_with("sqlite_") => {
                return Err(PyValueError::new_err(format!(
                    "reset({key:?}) isn't supported: SQLite's settings are per process here (D-023)"
                )));
            }
            "eviction_policy" => Self::Policy(match given {
                Some(v) => Some(parse_policy(v.extract()?)?),
                None => None,
            }),
            "statistics" => {
                return Err(PyValueError::new_err("statistics are turned on and off with stats(enable=...)"));
            }
            _ => {
                return Err(PyValueError::new_err(format!(
                    "reset() takes 'size_limit', 'cull_limit' or 'eviction_policy', not {key:?}"
                )));
            }
        };
        if !update {
            return Err(PyValueError::new_err(
                "reset(..., update=False) isn't supported: these settings have no SQLite pragma to skip",
            ));
        }
        Ok(parsed)
    }

    pub(crate) fn requested(&self) -> Requested {
        match *self {
            Self::Size(value) => Requested { size_limit: value, cull_limit: None, eviction_policy: None },
            Self::Cull(value) => Requested { size_limit: None, cull_limit: value, eviction_policy: None },
            Self::Policy(value) => Requested { size_limit: None, cull_limit: None, eviction_policy: value },
        }
    }

    /// The setting `reset` returns: an int for a limit, the policy's name for `eviction_policy`.
    pub(crate) fn of(&self, py: Python<'_>, limits: diskcache_oxide_core::open::Limits) -> PyResult<Py<PyAny>> {
        Ok(match self {
            Self::Size(_) => limits.size_limit.into_pyobject(py)?.into_any().unbind(),
            Self::Cull(_) => limits.cull_limit.into_pyobject(py)?.into_any().unbind(),
            Self::Policy(_) => limits.eviction_policy.name().into_pyobject(py)?.into_any().unbind(),
        })
    }

    pub(crate) fn is_read(&self) -> bool {
        matches!(self, Self::Size(None) | Self::Cull(None) | Self::Policy(None))
    }
}

/// `check()`'s findings as diskcache returns them: `warnings.WarningMessage` objects, made directly
/// (no `catch_warnings`, which swaps global state and obeys `-W error`), with diskcache's messages.
pub(crate) fn findings_to_warnings(
    py: Python<'_>,
    findings: Vec<diskcache_oxide_core::check::Finding>,
) -> PyResult<Py<pyo3::types::PyList>> {
    use diskcache_oxide_core::check::Finding;
    let warnings = py.import("warnings")?;
    let message_type = warnings.getattr("WarningMessage")?;
    let user = py.get_type::<pyo3::exceptions::PyUserWarning>().into_any();
    let unknown = py.import("diskcache_oxide._warnings")?.getattr("UnknownFileWarning")?;
    let out = pyo3::types::PyList::empty(py);
    for finding in findings {
        let (text, category) = match finding {
            Finding::Integrity(line) => (line, &user),
            Finding::MissingFile { path, .. } => (format!("file not found: {}", path.display()), &user),
            Finding::WrongSize { path, expected, actual, .. } => {
                (format!("wrong file size: {}, {actual} != {expected}", path.display()), &user)
            }
            Finding::BadFilename { name, .. } => (format!("damaged file name in a row: {name:?}"), &user),
            Finding::UnknownFile { path, .. } => (format!("unknown file: {}", path.display()), &unknown),
            Finding::Count { stored, actual, .. } => {
                (format!("Settings.count != COUNT(Cache.key); {stored} != {actual}"), &user)
            }
            Finding::Size { stored, actual, .. } => {
                (format!("Settings.size != SUM(Cache.size); {stored} != {actual}"), &user)
            }
        };
        let message = category.call1((text,))?;
        out.append(message_type.call1((message, category, "diskcache_oxide", 0))?)?;
    }
    Ok(out.unbind())
}

/// A lazy iterator over a cache's keys, one page per fetch.
#[pyclass(frozen, module = "diskcache_oxide")]
pub(crate) struct KeyIterator {
    cache: Py<Cache>,
    order: Order,
    prefix: Option<String>,
    state: Mutex<IterState>,
}

#[derive(Default)]
struct IterState {
    buffer: VecDeque<OwnedKey>,
    cursor: Cursor,
    done: bool,
}

impl KeyIterator {
    fn new(cache: Py<Cache>, order: Order, prefix: Option<String>) -> Self {
        Self { cache, order, prefix, state: Mutex::new(IterState::default()) }
    }
}

#[pymethods]
impl KeyIterator {
    /// Lets the garbage collector see the cache it holds: an iterator kept by something the cache
    /// refers to (its `clock` or `on_decode_error`) would otherwise leak both, with the handle open.
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        visit.call(&self.cache)
    }

    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(&self, py: Python<'_>) -> PyResult<Option<Py<PyAny>>> {
        // Never wait for the state while attached: its holder may be waiting for a pending fork
        // with the interpreter released, and could then never get it back (the gate's rules).
        // Another thread inside `next()` is an error instead, as for a generator (upstream's
        // iterators are generators).
        let mut state = match self.state.try_lock() {
            Ok(state) => state,
            Err(TryLockError::Poisoned(poisoned)) => poisoned.into_inner(),
            Err(TryLockError::WouldBlock) => return Err(PyValueError::new_err("iterator already executing")),
        };
        if let Some(key) = state.buffer.pop_front() {
            return codec::key::decode_owned(py, &key).map(Some);
        }
        if state.done {
            return Ok(None);
        }
        let cache = self.cache.get();
        let (keys, next) = cache
            .handle
            .run(cache.config.timeout, |timeout| {
                cache.handle.store.keys_page(timeout, self.order, self.prefix.as_deref(), &state.cursor)
            })
            .map_err(to_py_err)?;
        if keys.is_empty() {
            state.done = true;
            return Ok(None);
        }
        state.cursor = next;
        state.buffer.extend(keys);
        state.buffer.pop_front().map(|key| codec::key::decode_owned(py, &key)).transpose()
    }
}

/// Where a [`Transaction`] is. Only `__enter__` leaves `Ready`, and `Done` is final.
#[derive(Debug)]
enum TxState {
    /// Not entered yet.
    Ready,
    /// `__enter__` is beginning the transaction, with the mutex released: any other `__enter__` or
    /// `__exit__` meanwhile raises.
    Entering,
    /// This `Transaction` holds the store's write transaction.
    Held(HeldTransaction),
    /// Entered on a thread already holding a transaction on this store: it joined that one, and
    /// its exit neither commits nor rolls back.
    PassThrough,
    /// An `__exit__` is committing or rolling back, with the mutex released. Another exit with no
    /// exception waits (on `ended`) for the outcome; one with an exception returns at once.
    Ending,
    /// Exited. `committed` is true only for a commit that returned `Ok`, and for a pass-through: an
    /// exit with no exception raises `TransactionAborted` unless it's true.
    Done { committed: bool },
}

impl TxState {
    fn name(&self) -> &'static str {
        match self {
            Self::Ready => "Ready",
            Self::Entering => "Entering",
            Self::Held(_) => "Held",
            Self::PassThrough => "PassThrough",
            Self::Ending => "Ending",
            Self::Done { committed: true } => "Done(committed)",
            Self::Done { committed: false } => "Done(rolled back)",
        }
    }
}

/// What a [`Transaction`] holds its store's transaction for: a `Cache`, or one shard of a
/// `FanoutCache`. Either way it's one store, and `__enter__` hands back the cache object itself.
enum TxCache {
    Plain(Py<Cache>),
    Shard(Py<FanoutCache>, usize),
}

impl TxCache {
    fn handle(&self) -> &StoreHandle {
        match self {
            Self::Plain(cache) => &cache.get().handle,
            Self::Shard(cache, shard) => cache.get().shard_handle(*shard),
        }
    }

    fn config(&self) -> &HandleConfig {
        match self {
            Self::Plain(cache) => &cache.get().config,
            Self::Shard(cache, _) => cache.get().config(),
        }
    }

    fn object(&self, py: Python<'_>) -> Py<PyAny> {
        match self {
            Self::Plain(cache) => cache.clone_ref(py).into_any(),
            Self::Shard(cache, _) => cache.clone_ref(py).into_any(),
        }
    }

    fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        match self {
            Self::Plain(cache) => visit.call(cache),
            Self::Shard(cache, _) => visit.call(cache),
        }
    }
}

/// The context manager returned by `Cache.transact()` and `FanoutCache.transact(key=...)`.
/// Single use: it's entered once and exited once. It may be exited on another thread (D-031).
#[pyclass(frozen, module = "diskcache_oxide")]
pub(crate) struct Transaction {
    cache: TxCache,
    /// Retry a `BEGIN` that timed out, between attempts checking for signals.
    retry: bool,
    /// Taken attached, and never held across `begin_hold` or `finish`, which wait detached:
    /// `Entering` and `Ending` keep a second caller out meanwhile.
    state: Mutex<TxState>,
    /// Notified when `Ending` becomes `Done`.
    ended: Condvar,
}

impl Transaction {
    fn new(cache: Py<Cache>, retry: bool) -> Self {
        Self::with(TxCache::Plain(cache), retry)
    }

    /// A transaction on one shard of a `FanoutCache`.
    fn on_shard(cache: Py<FanoutCache>, shard: usize, retry: bool) -> Self {
        Self::with(TxCache::Shard(cache, shard), retry)
    }

    fn with(cache: TxCache, retry: bool) -> Self {
        Self { cache, retry, state: Mutex::new(TxState::Ready), ended: Condvar::new() }
    }

    fn lock_state(&self) -> std::sync::MutexGuard<'_, TxState> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Begins the transaction, or joins the one this thread already holds on the store. Never
    /// through `attempt`, so a durable handle raises rather than swallowing a failed `BEGIN`.
    fn begin(&self, py: Python<'_>) -> PyResult<TxState> {
        // A shard's transaction refuses to start while this thread holds another shard's (M3).
        if let TxCache::Shard(cache, shard) = &self.cache
            && Store::any_held_in_process()
        {
            cache.get().refuse_other_shards(*shard)?;
        }
        let (handle, timeout) = (self.cache.handle(), self.cache.config().timeout);
        // Per store, so a second handle on the directory passes through too. An inherited
        // transaction doesn't count: a child's `begin_hold` then refuses (D-020).
        if handle.store.held_by_this_thread() {
            return Ok(TxState::PassThrough);
        }
        loop {
            let started = Instant::now();
            let began = crate::detach(py, || handle.run(timeout, |timeout| handle.store.begin_hold(timeout)));
            match began {
                Ok(held) => return Ok(TxState::Held(held)),
                Err(Error::Timeout { .. }) if self.retry => pause_before_retry(py, started)?,
                Err(error) => return Err(to_py_err(error)),
            }
        }
    }
}

impl Transaction {
    /// Waits, detached, up to the cache's `timeout` for another thread's end to finish, and returns
    /// whether it committed, or `None` if it's still running.
    fn wait_for_end(&self, py: Python<'_>) -> Option<bool> {
        let timeout = self.cache.config().timeout;
        crate::detach(py, || {
            let state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
            let (state, _) = self
                .ended
                .wait_timeout_while(state, timeout, |s| matches!(s, TxState::Ending))
                .unwrap_or_else(PoisonError::into_inner);
            match *state {
                TxState::Done { committed } => Some(committed),
                _ => None,
            }
        })
    }
}

#[pymethods]
impl Transaction {
    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.cache.traverse(&visit)
    }

    fn __repr__(&self) -> String {
        match &self.cache {
            TxCache::Plain(_) => format!("<diskcache_oxide.Transaction {}>", self.lock_state().name()),
            TxCache::Shard(_, shard) => {
                format!("<diskcache_oxide.Transaction {} on shard {shard}>", self.lock_state().name())
            }
        }
    }

    fn __enter__(&self, py: Python<'_>) -> PyResult<Py<PyAny>> {
        {
            let mut state = self.lock_state();
            match *state {
                TxState::Ready => *state = TxState::Entering,
                TxState::Entering => {
                    return Err(PyRuntimeError::new_err("this transaction is already being entered on another thread"));
                }
                _ => return Err(PyRuntimeError::new_err("a transaction can be entered only once")),
            }
        }
        let began = self.begin(py);
        let mut state = self.lock_state();
        // Nothing else leaves `Entering`, so this drops nothing.
        match began {
            Ok(entered) => {
                *state = entered;
                Ok(self.cache.object(py))
            }
            Err(error) => {
                *state = TxState::Ready;
                Err(error)
            }
        }
    }

    #[pyo3(signature = (exc_type, _exc_value, _traceback))]
    fn __exit__(
        &self,
        py: Python<'_>,
        exc_type: Option<&Bound<'_, PyAny>>,
        _exc_value: Option<&Bound<'_, PyAny>>,
        _traceback: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let commit = exc_type.is_none();
        let mut state = self.lock_state();
        let taken = match &*state {
            TxState::Ready => return Err(PyRuntimeError::new_err("this transaction was never entered")),
            TxState::Entering => {
                return Err(PyRuntimeError::new_err("this transaction is still being entered on another thread"));
            }
            // A second exit, or the owner's exit after another thread ended it.
            TxState::Done { committed } => {
                if commit && !*committed {
                    return Err(not_committed());
                }
                return Ok(false);
            }
            // Another thread is ending it. An exception passing needs no outcome; otherwise this
            // exit reports whether that end committed, once it has.
            TxState::Ending => {
                drop(state);
                if !commit {
                    return Ok(false);
                }
                return match self.wait_for_end(py) {
                    Some(true) => Ok(false),
                    Some(false) => Err(not_committed()),
                    None => Err(crate::Timeout::new_err(
                        "this transaction is still being ended on another thread; whether it committed is unknown",
                    )),
                };
            }
            TxState::PassThrough => {
                *state = TxState::Done { committed: true };
                return Ok(false);
            }
            TxState::Held(_) => std::mem::replace(&mut *state, TxState::Ending),
        };
        drop(state);
        let TxState::Held(held) = taken else { unreachable!("only `Held` is taken") };
        // Outside the mutex: a commit may wait (detached) for the owner's operation.
        let result = held.finish(commit);
        *self.lock_state() = TxState::Done { committed: commit && result.is_ok() };
        self.ended.notify_all();
        result.map_err(to_py_err)?;
        Ok(false)
    }
}

/// The owner's exit, with no exception, of a transaction that ended without committing: another
/// thread ended it (D-031), an earlier exit did, or it was rolled back after an error. The core's
/// `TransactionAborted` message ("rolled back after an error") is only right for the last.
fn not_committed() -> PyErr {
    crate::TransactionAborted::new_err(
        "the transaction did not commit: another thread or an earlier exit ended it, or it was rolled back after an error",
    )
}

/// Converts a row's value column for the codec and decodes it. Returns the decode result rather
/// than failing the store call, so a Python error (e.g. `UnsafeValueError`) passes through intact.
fn decode_value(py: Python<'_>, codec: &Codec, mode: i64, value: ValueRef<'_>) -> PyResult<Py<PyAny>> {
    let stored = match value {
        ValueRef::Blob(bytes) | ValueRef::Text(bytes) => codec::Stored::Blob(bytes),
        ValueRef::Integer(n) => codec::Stored::Int(n),
        ValueRef::Real(x) => codec::Stored::Float(x),
        // A value file the store didn't read because this handle refuses its mode.
        ValueRef::Null if codec.refuses(mode) => return Err(codec::refusal(mode)),
        ValueRef::Null => return Err(crate::CorruptValueError::new_err(format!("mode {mode} row has no value"))),
    };
    codec.decode(py, mode, stored)
}

/// What a read does with a value it can't decode: the `on_decode_error=` argument.
pub(crate) enum DecodePolicy {
    /// `'raise'`: raise the error (the default).
    Raise,
    /// `'miss'`: a miss, logged as a warning on the `diskcache_oxide` logger.
    Miss,
    /// A callable: a miss, reported by calling it with `(key, exc)` instead of the warning.
    Report(Py<PyAny>),
}

impl<'a, 'py> FromPyObject<'a, 'py> for DecodePolicy {
    type Error = PyErr;

    fn extract(obj: pyo3::Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        if let Ok(name) = obj.cast::<PyString>() {
            return match name.to_str()? {
                "raise" => Ok(DecodePolicy::Raise),
                "miss" => Ok(DecodePolicy::Miss),
                other => Err(PyValueError::new_err(format!(
                    "on_decode_error must be 'raise', 'miss' or a callable, got {other:?}"
                ))),
            };
        }
        if obj.is_callable() {
            return Ok(DecodePolicy::Report(obj.to_owned().unbind()));
        }
        Err(PyTypeError::new_err(format!(
            "on_decode_error must be 'raise', 'miss' or a callable, not {}",
            obj.get_type().name()?
        )))
    }
}

/// Which row a failed decode came from, for what `on_decode_error='miss'` can do about it.
enum Row {
    /// Still stored, as this version.
    Stored(Version),
    /// Already deleted (by `pop`).
    Removed,
    /// Deliberately not deleted (a refused `pop`).
    Kept,
}

/// What a read of a live row found: the value, or the row's mode and why it couldn't be decoded.
type Decoded = Hit<Result<Py<PyAny>, (i64, PyErr)>>;

/// A live value read or popped: the value, when it expires, and its tag if one was asked for.
type Found = (Py<PyAny>, Option<f64>, Option<OwnedKey>);

/// What `get` and `pop` return besides the value, in diskcache's order: `(value, expire_time,
/// tag)` with both, either one alone in a pair, or just the value.
#[derive(Clone, Copy)]
struct Wants {
    expire_time: bool,
    tag: bool,
}

impl Wants {
    fn answer(
        self,
        py: Python<'_>,
        value: Py<PyAny>,
        expires: Option<f64>,
        tag: Option<&OwnedKey>,
    ) -> PyResult<Py<PyAny>> {
        if !self.expire_time && !self.tag {
            return Ok(value);
        }
        let mut items = Vec::with_capacity(3);
        items.push(value);
        if self.expire_time {
            items.push(expires.into_pyobject(py)?.into_any().unbind());
        }
        if self.tag {
            items.push(match tag {
                Some(tag) => codec::key::decode_owned(py, tag)?,
                None => py.None(),
            });
        }
        Ok(PyTuple::new(py, items)?.into_any().unbind())
    }
}

/// What kind of decode failure an error is (plan 08, step 2b).
enum Failure {
    /// The bytes can never be read, by this or any build: a miss, and the row is deleted.
    Unreadable,
    /// This build can't read it, but another might: a miss, and the row stays (D-001).
    Foreign,
    /// An outage reaching a durable handle through a pickle hook: a miss, and the row stays.
    Transient,
    /// Not about the stored bytes, or a configuration error: raised as is.
    Passthrough,
}

fn classify(py: Python<'_>, mode: i64, e: &PyErr) -> Failure {
    if !e.is_instance_of::<PyException>(py) || e.is_instance_of::<crate::UnsafeValueError>(py) {
        // KeyboardInterrupt and friends; and a pickle on a pickle=False handle, a configuration
        // bug that a miss would hide on every read.
        Failure::Passthrough
    } else if e.is_instance_of::<crate::ForeignValueError>(py) {
        Failure::Foreign
    } else if e.is_instance_of::<crate::CorruptValueError>(py) {
        Failure::Unreadable
    } else if codec::is_pickle(mode)
        && !(e.is_instance_of::<PyMemoryError>(py)
            || e.is_instance_of::<PyRecursionError>(py)
            || e.is_instance_of::<PyOSError>(py))
    {
        // Unpickling raised: a class moved or renamed, or a failing `__setstate__`. Transient
        // errors, and `Timeout` or I/O errors from a cache call inside a pickle hook, are raised.
        Failure::Unreadable
    } else {
        Failure::Passthrough
    }
}

/// The `diskcache_oxide` logger, looked up once.
pub(crate) fn logger(py: Python<'_>) -> PyResult<&Bound<'_, PyAny>> {
    static LOGGER: PyOnceLock<Py<PyAny>> = PyOnceLock::new();
    LOGGER
        .get_or_try_init(py, || {
            Ok::<_, PyErr>(py.import("logging")?.call_method1("getLogger", ("diskcache_oxide",))?.unbind())
        })
        .map(|log| log.bind(py))
}

thread_local! {
    /// Set while this thread is inside an `on_decode_error` callable.
    static REPORTING: std::cell::Cell<bool> = const { std::cell::Cell::new(false) };
}

/// Marks this thread as reporting a decode failure until dropped.
struct Reporting;

impl Reporting {
    /// None if this thread is already reporting one.
    fn enter() -> Option<Reporting> {
        if REPORTING.replace(true) { None } else { Some(Reporting) }
    }
}

impl Drop for Reporting {
    fn drop(&mut self) {
        REPORTING.set(false);
    }
}

/// The core value for an encoded payload, borrowing its bytes (no copy before SQLite's own).
fn stored_value<'a>(payload: &'a Payload<'_>, mode: u8) -> PyResult<Value<'a>> {
    Ok(match payload {
        Payload::Int(n) => Value::Int(*n),
        Payload::Float(x) => Value::Real(*x),
        Payload::Str(_) | Payload::Bytes(_) => {
            Value::Bytes { mode: i64::from(mode), data: payload.as_blob()?.unwrap_or_default() }
        }
    })
}

/// A number of seconds; `bool` is rejected (D-014).
fn seconds(name: &str, value: &Bound<'_, PyAny>) -> PyResult<f64> {
    if value.is_instance_of::<PyBool>() || !(value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>()) {
        return Err(PyTypeError::new_err(format!(
            "{name} must be a number of seconds or None, not {}",
            value.get_type().name()?
        )));
    }
    value.extract::<f64>()
}

/// A validated timeout in seconds as a `Duration`.
fn timeout_duration(seconds: f64) -> PyResult<Duration> {
    if !seconds.is_finite() || seconds < 0.0 {
        return Err(PyValueError::new_err(format!("timeout must be a non-negative number of seconds, got {seconds}")));
    }
    // Clamped before converting: `Duration::from_secs_f64` panics above about 1.8e19 seconds.
    Ok(Duration::from_secs_f64(seconds.min(lockwait::MAX_TIMEOUT.as_secs_f64())))
}

fn optional_seconds(name: &str, value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<f64>> {
    match value {
        None => Ok(None),
        Some(v) if v.is_none() => Ok(None),
        Some(v) => seconds(name, v).map(Some),
    }
}

/// An optional whole-number setting; `bool` is rejected (D-014).
fn whole(name: &str, value: Option<&Bound<'_, PyAny>>) -> PyResult<Option<i64>> {
    match value {
        None => Ok(None),
        Some(v) if v.is_none() => Ok(None),
        Some(v) if v.is_instance_of::<PyBool>() || !v.is_instance_of::<PyInt>() => {
            Err(PyTypeError::new_err(format!("{name} must be an int or None, not {}", v.get_type().name()?)))
        }
        Some(v) => v.extract::<i64>().map(Some),
    }
}

/// The `delta` argument of `incr`/`decr`.
#[derive(Clone, Copy)]
pub(crate) struct Delta(Number);

impl<'a, 'py> FromPyObject<'a, 'py> for Delta {
    type Error = PyErr;

    fn extract(obj: pyo3::Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        number("delta", &obj).map(Delta)
    }
}

/// The `default` argument of `incr`/`decr`. A separate type so an explicit `None` (raise
/// `KeyError` on a missing key) is told apart from the argument being left out (start from 0):
/// PyO3 turns both into `None` for an `Option` parameter.
#[derive(Clone, Copy)]
pub(crate) struct IncrDefault(Option<Number>);

impl<'a, 'py> FromPyObject<'a, 'py> for IncrDefault {
    type Error = PyErr;

    fn extract(obj: pyo3::Borrowed<'a, 'py, PyAny>) -> PyResult<Self> {
        if obj.is_none() { Ok(IncrDefault(None)) } else { number("default", &obj).map(|n| IncrDefault(Some(n))) }
    }
}

/// An `incr`/`decr` operand: an exact int (fits 64 bits) or a float.
fn number(name: &str, value: &Bound<'_, PyAny>) -> PyResult<Number> {
    if value.is_exact_instance_of::<PyInt>() {
        return value.extract::<i64>().map(Number::Int);
    }
    if value.is_exact_instance_of::<PyFloat>() {
        return value.extract::<f64>().map(Number::Real);
    }
    Err(PyTypeError::new_err(format!("{name} must be an int or float, not {}", value.get_type().name()?)))
}
