//! `diskcache_oxide.Cache`: the frozen pyclass over a shared core `Store` (04-design.md section 2).
//!
//! The class is the public object itself, with no Python wrapper frame (Spike 2). Every operation:
//! validates and encodes its arguments, reads the clock once (`clock=` or the native clock), and
//! only then calls into the store, which takes the write lock, gate and connection. Decoding a
//! small value happens while the connection is held, straight from SQLite's row buffer, and runs
//! no user code.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use std::collections::VecDeque;

use diskcache_oxide_core::maintenance::{Cursor, Order};
use diskcache_oxide_core::ops::{Number, Value};
use diskcache_oxide_core::store::{
    DEFAULT_CACHE_PAGES, DEFAULT_MAX_CONNECTIONS, DEFAULT_WAL_SIZE_LIMIT, HeldTransaction, OpenOptions, Store,
};
use diskcache_oxide_core::{Error, ValueRef, lockwait, open::Requested};
use pyo3::exceptions::{PyKeyError, PyOverflowError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyFloat, PyInt, PyString, PyTuple};

use crate::codec::{self, Payload, Serializer};
use crate::to_py_err;

/// A disk cache backed by SQLite, shared safely across threads and processes.
#[pyclass(frozen, module = "diskcache_oxide")]
pub(crate) struct Cache {
    store: Arc<Store>,
    directory: String,
    timeout: Duration,
    serializer: Serializer,
    pickle: bool,
    clock: Option<Py<PyAny>>,
    closed: AtomicBool,
}

#[pymethods]
impl Cache {
    #[new]
    #[pyo3(signature = (
        directory,
        timeout = None,
        *,
        size_limit = None,
        cull_limit = None,
        serializer = "msgpack",
        pickle = false,
        clock = None,
        max_connections = None,
        sqlite_cache_size = None,
        wal_size_limit = None,
        wal_checkpoint = "manual",
    ))]
    #[allow(clippy::too_many_arguments)]
    fn new(
        py: Python<'_>,
        directory: PathBuf,
        timeout: Option<&Bound<'_, PyAny>>,
        size_limit: Option<&Bound<'_, PyAny>>,
        cull_limit: Option<&Bound<'_, PyAny>>,
        serializer: &str,
        pickle: bool,
        clock: Option<Py<PyAny>>,
        max_connections: Option<&Bound<'_, PyAny>>,
        sqlite_cache_size: Option<&Bound<'_, PyAny>>,
        wal_size_limit: Option<&Bound<'_, PyAny>>,
        wal_checkpoint: &str,
    ) -> PyResult<Self> {
        let timeout = match timeout {
            Some(t) => seconds("timeout", t)?,
            None => 60.0,
        };
        if !timeout.is_finite() || timeout < 0.0 {
            return Err(PyValueError::new_err(format!(
                "timeout must be a non-negative number of seconds, got {timeout}"
            )));
        }
        // Clamped before converting: `Duration::from_secs_f64` panics above about 1.8e19 seconds.
        let timeout = Duration::from_secs_f64(timeout.min(lockwait::MAX_TIMEOUT.as_secs_f64()));
        let requested =
            Requested { size_limit: whole("size_limit", size_limit)?, cull_limit: whole("cull_limit", cull_limit)? };
        let max_connections = match whole("max_connections", max_connections)? {
            Some(n) if n < 1 => return Err(PyValueError::new_err("max_connections must be at least 1")),
            Some(n) => n.unsigned_abs(),
            None => DEFAULT_MAX_CONNECTIONS,
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
        let options = OpenOptions {
            directory: directory.clone(),
            timeout,
            requested,
            max_connections,
            cache_pages,
            wal_size_limit,
            auto_checkpoint,
        };
        let store = py.detach(|| Store::open(&options)).map_err(to_py_err)?;
        Ok(Self {
            store,
            directory: directory.to_string_lossy().into_owned(),
            timeout,
            serializer,
            pickle,
            clock,
            closed: AtomicBool::new(false),
        })
    }

    // -- attributes ---------------------------------------------------------------------------

    #[getter]
    fn directory(&self) -> &str {
        &self.directory
    }

    #[getter]
    fn timeout(&self) -> f64 {
        self.timeout.as_secs_f64()
    }

    #[getter]
    fn size_limit(&self) -> i64 {
        self.store.cached_limits().size_limit
    }

    #[getter]
    fn cull_limit(&self) -> i64 {
        self.store.cached_limits().cull_limit
    }

    /// Always True for a real cache (a `NullCache` is False).
    #[getter]
    #[allow(clippy::unused_self)] // A Python attribute: it needs `self` even though it's constant.
    fn enabled(&self) -> bool {
        true
    }

    /// Test hook: what forced WAL checkpoints have done on this cache's store.
    fn _wal_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        let s = self.store.wal_stats();
        let d = pyo3::types::PyDict::new(py);
        for (k, v) in [
            ("checks", s.checks),
            ("attempts", s.attempts),
            ("successes", s.successes),
            ("busy", s.busy),
            ("errors", s.errors),
            ("no_slot", s.no_slot),
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

    fn __repr__(&self) -> String {
        format!("Cache({:?})", self.directory)
    }

    // -- reads --------------------------------------------------------------------------------

    #[pyo3(signature = (key, default = None, *, expire_time = false))]
    fn get(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        default: Option<Py<PyAny>>,
        expire_time: bool,
    ) -> PyResult<Py<PyAny>> {
        let key = str_key(key)?;
        let now = self.now(py)?;
        let found = self.lookup(py, key, now)?;
        let default = default.unwrap_or_else(|| py.None());
        match (found, expire_time) {
            (Some((value, _)), false) => Ok(value),
            (None, false) => Ok(default),
            (Some((value, expires)), true) => {
                Ok(PyTuple::new(py, [value, expires.into_pyobject(py)?.into_any().unbind()])?.into_any().unbind())
            }
            (None, true) => Ok(PyTuple::new(py, [default, py.None()])?.into_any().unbind()),
        }
    }

    fn __getitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        let now = self.now(py)?;
        match self.lookup(py, name, now)? {
            Some((value, _)) => Ok(value),
            None => Err(PyKeyError::new_err(key.clone().unbind())),
        }
    }

    fn __contains__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        let key = str_key(key)?;
        let now = self.now(py)?;
        self.ensure_open();
        self.store.contains(self.timeout, key, now).map_err(to_py_err)
    }

    fn __len__(&self) -> PyResult<usize> {
        self.ensure_open();
        let count = self.store.len(self.timeout).map_err(to_py_err)?;
        Ok(usize::try_from(count).unwrap_or(0))
    }

    /// Always True: a cache is usable even when empty (D-009).
    #[allow(clippy::unused_self)] // A Python protocol method: it needs `self`.
    fn __bool__(&self) -> bool {
        true
    }

    fn volume(&self) -> PyResult<i64> {
        self.ensure_open();
        self.store.volume(self.timeout).map_err(to_py_err)
    }

    /// Checkpoints the write-ahead log and truncates it to zero (D-024), retrying until `timeout`.
    /// True if the WAL was reset; False if readers or writers in other processes kept it busy.
    /// `timeout`: how long to keep trying, in seconds (default: the cache's timeout).
    #[pyo3(signature = (timeout = None))]
    fn checkpoint(&self, timeout: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
        let timeout = match timeout {
            Some(t) if !t.is_none() => {
                let t = seconds("timeout", t)?;
                if !t.is_finite() || t < 0.0 {
                    return Err(PyValueError::new_err(format!(
                        "timeout must be a non-negative number of seconds, got {t}"
                    )));
                }
                Duration::from_secs_f64(t.min(lockwait::MAX_TIMEOUT.as_secs_f64()))
            }
            _ => self.timeout,
        };
        self.ensure_open();
        self.store.checkpoint(timeout).map_err(to_py_err)
    }

    // -- writes -------------------------------------------------------------------------------

    #[pyo3(signature = (key, value, expire = None))]
    fn set(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        expire: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let key = str_key(key)?;
        let expire = optional_seconds("expire", expire)?;
        let encoded = codec::encode(py, value, self.serializer, self.pickle)?;
        let value = stored_value(&encoded.payload, encoded.mode.as_u8())?;
        let now = self.now(py)?;
        self.ensure_open();
        self.store.set(self.timeout, key, value, expire, now).map_err(to_py_err)
    }

    fn __setitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>, value: &Bound<'_, PyAny>) -> PyResult<()> {
        self.set(py, key, value, None).map(|_| ())
    }

    #[pyo3(signature = (key, value, expire = None))]
    fn add(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        expire: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let key = str_key(key)?;
        let expire = optional_seconds("expire", expire)?;
        let encoded = codec::encode(py, value, self.serializer, self.pickle)?;
        let value = stored_value(&encoded.payload, encoded.mode.as_u8())?;
        let now = self.now(py)?;
        self.ensure_open();
        self.store.add(self.timeout, key, value, expire, now).map_err(to_py_err)
    }

    fn delete(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        let key = str_key(key)?;
        let now = self.now(py)?;
        self.ensure_open();
        self.store.delete(self.timeout, key, now).map_err(to_py_err)
    }

    fn __delitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<()> {
        if self.delete(py, key)? { Ok(()) } else { Err(PyKeyError::new_err(key.clone().unbind())) }
    }

    #[pyo3(signature = (key, default = None))]
    fn pop(&self, py: Python<'_>, key: &Bound<'_, PyAny>, default: Option<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let key = str_key(key)?;
        let now = self.now(py)?;
        self.ensure_open();
        let allow_pickle = self.pickle;
        let popped = self
            .store
            .pop_with(self.timeout, key, now, |mode, value| Ok(decode_value(py, mode, value, allow_pickle)))
            .map_err(to_py_err)?;
        match popped {
            Some(value) => value,
            None => Ok(default.unwrap_or_else(|| py.None())),
        }
    }

    #[pyo3(signature = (key, expire = None))]
    fn touch(&self, py: Python<'_>, key: &Bound<'_, PyAny>, expire: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
        let key = str_key(key)?;
        let expire = optional_seconds("expire", expire)?;
        let now = self.now(py)?;
        self.ensure_open();
        self.store.touch(self.timeout, key, expire, now).map_err(to_py_err)
    }

    #[pyo3(signature = (key, delta = Delta(Number::Int(1)), default = IncrDefault(Some(Number::Int(0)))))]
    fn incr(&self, py: Python<'_>, key: &Bound<'_, PyAny>, delta: Delta, default: IncrDefault) -> PyResult<Py<PyAny>> {
        self.add_number(py, key, delta, default, false)
    }

    #[pyo3(signature = (key, delta = Delta(Number::Int(1)), default = IncrDefault(Some(Number::Int(0)))))]
    fn decr(&self, py: Python<'_>, key: &Bound<'_, PyAny>, delta: Delta, default: IncrDefault) -> PyResult<Py<PyAny>> {
        self.add_number(py, key, delta, default, true)
    }

    // -- maintenance and iteration -------------------------------------------------------------

    /// Removes every expired entry. Returns how many it removed.
    fn expire(&self, py: Python<'_>) -> PyResult<u64> {
        let now = self.now(py)?;
        self.ensure_open();
        py.detach(|| self.store.expire(self.timeout, now)).map_err(to_py_err)
    }

    /// Removes every entry stored before the call. Returns how many it removed.
    fn clear(&self, py: Python<'_>) -> PyResult<u64> {
        self.ensure_open();
        py.detach(|| self.store.clear(self.timeout)).map_err(to_py_err)
    }

    /// Removes expired entries, then evicts until the cache is under `size_limit`. Returns how many
    /// it removed.
    fn cull(&self, py: Python<'_>) -> PyResult<u64> {
        let now = self.now(py)?;
        self.ensure_open();
        py.detach(|| self.store.cull(self.timeout, now)).map_err(to_py_err)
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

    // -- lifecycle ----------------------------------------------------------------------------

    /// Releases this handle. When the last handle on a directory closes, every connection closes
    /// (D-018). Using the cache again afterwards reopens it, as diskcache does.
    /// Closes this handle. With `checkpoint` (the default), a WAL left larger than its trim size
    /// (half of `wal_size_limit`) is checkpointed first, waiting at most 1 s (D-024). A small WAL,
    /// the usual case, costs nothing extra.
    #[pyo3(signature = (*, checkpoint = true))]
    fn close(&self, checkpoint: bool) {
        if !self.closed.swap(true, Ordering::AcqRel) {
            if checkpoint {
                self.store.checkpoint_on_close(self.timeout.min(Duration::from_secs(1)));
            }
            self.store.release_handle();
        }
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[pyo3(signature = (exc_type, exc, tb, /))]
    fn __exit__(&self, exc_type: &Bound<'_, PyAny>, exc: &Bound<'_, PyAny>, tb: &Bound<'_, PyAny>) {
        let _ = (exc_type, exc, tb);
        self.close(true);
    }

    /// Test hook until `transact()` lands (Phase 6b): a context manager holding a write
    /// transaction. Operations by the same thread inside it run in the transaction.
    fn _hold_write_transaction(slf: Py<Self>) -> WriteHold {
        WriteHold { cache: slf, held: Mutex::new(None) }
    }
}

impl Cache {
    fn now(&self, py: Python<'_>) -> PyResult<f64> {
        match &self.clock {
            Some(clock) => clock.bind(py).call0()?.extract::<f64>(),
            None => Ok(SystemTime::now().duration_since(UNIX_EPOCH).map_or(0.0, |d| d.as_secs_f64())),
        }
    }

    /// A handle that was closed and is used again rejoins its store (diskcache reopens lazily).
    fn ensure_open(&self) {
        if self.closed.swap(false, Ordering::AcqRel) {
            self.store.add_handle();
        }
    }

    fn lookup(&self, py: Python<'_>, key: &str, now: f64) -> PyResult<Option<(Py<PyAny>, Option<f64>)>> {
        self.ensure_open();
        let allow_pickle = self.pickle;
        let found = self
            .store
            .get_with(self.timeout, key, now, |mode, value| Ok(decode_value(py, mode, value, allow_pickle)))
            .map_err(to_py_err)?;
        match found {
            Some((value, expires)) => Ok(Some((value?, expires))),
            None => Ok(None),
        }
    }

    fn add_number(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        Delta(mut delta): Delta,
        IncrDefault(default): IncrDefault,
        negate: bool,
    ) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        if negate {
            delta = match delta {
                Number::Int(n) => {
                    Number::Int(n.checked_neg().ok_or_else(|| PyOverflowError::new_err("integer overflow"))?)
                }
                Number::Real(x) => Number::Real(-x),
            };
        }
        let now = self.now(py)?;
        self.ensure_open();
        match self.store.incr(self.timeout, name, delta, default, now) {
            Ok(Number::Int(n)) => Ok(n.into_pyobject(py)?.into_any().unbind()),
            Ok(Number::Real(x)) => Ok(PyFloat::new(py, x).into_any().unbind()),
            Err(Error::KeyMissing) => Err(PyKeyError::new_err(key.clone().unbind())),
            Err(error) => Err(to_py_err(error)),
        }
    }
}

impl Drop for Cache {
    fn drop(&mut self) {
        if !self.closed.swap(true, Ordering::AcqRel) {
            self.store.release_handle();
        }
    }
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
    buffer: VecDeque<String>,
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
    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    fn __next__(&self) -> PyResult<Option<String>> {
        let mut state = self.state.lock().unwrap_or_else(PoisonError::into_inner);
        if let Some(key) = state.buffer.pop_front() {
            return Ok(Some(key));
        }
        if state.done {
            return Ok(None);
        }
        let cache = self.cache.get();
        cache.ensure_open();
        let (keys, next) = cache
            .store
            .keys_page(cache.timeout, self.order, self.prefix.as_deref(), &state.cursor)
            .map_err(to_py_err)?;
        if keys.is_empty() {
            state.done = true;
            return Ok(None);
        }
        state.cursor = next;
        state.buffer.extend(keys);
        Ok(state.buffer.pop_front())
    }
}

/// The context manager returned by `Cache._hold_write_transaction()`.
#[pyclass(frozen, module = "diskcache_oxide")]
pub(crate) struct WriteHold {
    cache: Py<Cache>,
    held: Mutex<Option<HeldTransaction>>,
}

#[pymethods]
impl WriteHold {
    fn __enter__(&self, py: Python<'_>) -> PyResult<()> {
        let cache = self.cache.get();
        cache.ensure_open();
        let held = py.detach(|| cache.store.begin_hold(cache.timeout)).map_err(to_py_err)?;
        *self.held.lock().unwrap_or_else(PoisonError::into_inner) = Some(held);
        Ok(())
    }

    #[pyo3(signature = (exc_type, _exc_value, _traceback))]
    fn __exit__(
        &self,
        exc_type: Option<&Bound<'_, PyAny>>,
        _exc_value: Option<&Bound<'_, PyAny>>,
        _traceback: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let held = self.held.lock().unwrap_or_else(PoisonError::into_inner).take();
        if let Some(held) = held {
            held.finish(exc_type.is_none()).map_err(to_py_err)?;
        }
        Ok(false)
    }
}

/// Converts a row's value column for the codec and decodes it. Returns the decode result rather
/// than failing the store call, so a Python error (e.g. `UnsafeValueError`) passes through intact.
fn decode_value(py: Python<'_>, mode: i64, value: ValueRef<'_>, allow_pickle: bool) -> PyResult<Py<PyAny>> {
    let stored = match value {
        ValueRef::Blob(bytes) | ValueRef::Text(bytes) => codec::Stored::Blob(bytes),
        ValueRef::Integer(n) => codec::Stored::Int(n),
        ValueRef::Real(x) => codec::Stored::Float(x),
        ValueRef::Null => return Err(crate::CorruptValueError::new_err(format!("mode {mode} row has no value"))),
    };
    codec::decode(py, mode, stored, allow_pickle)
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

/// Keys are `str` only for now (D-004).
fn str_key<'a>(key: &'a Bound<'_, PyAny>) -> PyResult<&'a str> {
    match key.cast_exact::<PyString>() {
        Ok(s) => s.to_str(),
        Err(_) => Err(PyTypeError::new_err(format!("cache keys must be str, not {}", key.get_type().name()?))),
    }
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
