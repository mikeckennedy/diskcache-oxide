//! Phase 1, Spikes 2 and 5 only: a `Cache` with just what the latency gate and the fork tests need
//! (`get`, `set` and `incr` for `str`, `bytes` and `int`, plus the `_hold_write_transaction` test
//! hook), exposed as the frozen pyclass itself (no Python wrapper frame). Replaced in Phase 2.

use std::path::PathBuf;
use std::time::Duration;

use diskcache_oxide_core::spike::{Incr, SpikeStore, StoredValue};
use pyo3::exceptions::{PyKeyError, PyOverflowError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyInt, PyString, PyTuple};

use crate::to_py_err;

#[pyclass(frozen, module = "diskcache_oxide")]
pub(crate) struct Cache {
    store: SpikeStore,
    /// Spike knob: release the interpreter around SQLite calls (`py.detach`) or not.
    detach: bool,
}

/// A decoded value copied out of the row, for the detached read path.
enum Owned {
    Str(String),
    Bytes(Vec<u8>),
    Int(i64),
}

#[pymethods]
impl Cache {
    #[new]
    #[pyo3(signature = (directory, timeout = 60.0, size_limit = None, *, spike_detach = false))]
    fn new(directory: PathBuf, timeout: f64, size_limit: Option<u64>, spike_detach: bool) -> PyResult<Self> {
        let _ = size_limit; // Accepted so the benchmark adapter can pass it; the spike has no culling.
        let timeout = Duration::try_from_secs_f64(timeout).map_err(|e| PyValueError::new_err(e.to_string()))?;
        let store = SpikeStore::open(&directory, timeout).map_err(to_py_err)?;
        Ok(Self { store, detach: spike_detach })
    }

    #[pyo3(signature = (key, default = None))]
    fn get(&self, py: Python<'_>, key: &str, default: Option<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let found = if self.detach {
            let owned = py.detach(|| self.store.get_with(key, to_owned)).map_err(to_py_err)?;
            owned.map(|value| value.and_then(|value| from_owned(py, value)))
        } else {
            // Builds the Python object straight from the row's bytes, with no intermediate copy.
            self.store.get_with(key, |value| decode(py, value)).map_err(to_py_err)?
        };
        match found {
            Some(value) => value,
            None => Ok(default.unwrap_or_else(|| py.None())),
        }
    }

    #[pyo3(signature = (key, value, expire = None))]
    fn set(&self, py: Python<'_>, key: &str, value: &Bound<'_, PyAny>, expire: Option<f64>) -> PyResult<bool> {
        let int;
        let stored = if let Ok(text) = value.cast::<PyString>() {
            StoredValue::Str(text.to_str()?.as_bytes())
        } else if let Ok(raw) = value.cast::<PyBytes>() {
            StoredValue::Bytes(raw.as_bytes())
        } else if value.is_instance_of::<PyInt>() && !value.is_instance_of::<PyBool>() {
            int = value.extract::<i64>()?;
            StoredValue::Int(int)
        } else {
            return Err(PyTypeError::new_err("the spike cache stores only str, bytes and int values"));
        };
        if self.detach {
            py.detach(|| self.store.set(key, stored, expire)).map_err(to_py_err)?;
        } else {
            self.store.set(key, stored, expire).map_err(to_py_err)?;
        }
        Ok(true)
    }

    /// Adds `delta` to the int at `key` atomically, starting from `default` (`KeyError` if `None`).
    #[pyo3(signature = (key, delta = 1, default = Some(0)), text_signature = "($self, key, delta=1, default=0)")]
    fn incr(&self, key: &str, delta: i64, default: Option<i64>) -> PyResult<i64> {
        match self.store.incr(key, delta, default).map_err(to_py_err)? {
            Incr::Value(value) => Ok(value),
            Incr::Missing => Err(PyKeyError::new_err(key.to_owned())),
            Incr::NotAnInteger => Err(PyTypeError::new_err(format!("the value at {key:?} is not an int"))),
            Incr::Overflow => Err(PyOverflowError::new_err("incr result does not fit in 64 bits")),
        }
    }

    /// Test hook until `transact()` lands: a context manager holding a write transaction
    /// (`BEGIN IMMEDIATE`) on a pinned connection until it exits. The entering thread's operations
    /// on this cache join it; it commits on a clean exit and rolls back on an exception.
    fn _hold_write_transaction(slf: Py<Self>) -> WriteHold {
        WriteHold { cache: slf }
    }

    fn __len__(&self) -> PyResult<usize> {
        let count = self.store.count().map_err(to_py_err)?;
        usize::try_from(count).map_err(|e| PyOverflowError::new_err(e.to_string()))
    }

    fn close(&self) {
        self.store.close();
    }

    fn __enter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    #[pyo3(signature = (*_args))]
    fn __exit__(&self, _args: &Bound<'_, PyTuple>) {
        self.store.close();
    }
}

/// The context manager returned by `Cache._hold_write_transaction()`.
#[pyclass(frozen, module = "diskcache_oxide")]
pub(crate) struct WriteHold {
    cache: Py<Cache>,
}

#[pymethods]
impl WriteHold {
    fn __enter__(&self) -> PyResult<()> {
        self.cache.get().store.begin_hold().map_err(to_py_err)
    }

    #[pyo3(signature = (exc_type, _exc_value, _traceback))]
    fn __exit__(
        &self,
        exc_type: Option<&Bound<'_, PyAny>>,
        _exc_value: Option<&Bound<'_, PyAny>>,
        _traceback: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        self.cache.get().store.end_hold(exc_type.is_none()).map_err(to_py_err)?;
        Ok(false)
    }
}

fn decode(py: Python<'_>, value: StoredValue<'_>) -> PyResult<Py<PyAny>> {
    match value {
        StoredValue::Str(bytes) => {
            let text = std::str::from_utf8(bytes).map_err(|e| PyValueError::new_err(e.to_string()))?;
            Ok(PyString::new(py, text).into_any().unbind())
        }
        StoredValue::Bytes(bytes) => Ok(PyBytes::new(py, bytes).into_any().unbind()),
        StoredValue::Int(i) => Ok(i.into_pyobject(py)?.into_any().unbind()),
    }
}

fn to_owned(value: StoredValue<'_>) -> PyResult<Owned> {
    Ok(match value {
        StoredValue::Str(bytes) => {
            Owned::Str(std::str::from_utf8(bytes).map_err(|e| PyValueError::new_err(e.to_string()))?.to_owned())
        }
        StoredValue::Bytes(bytes) => Owned::Bytes(bytes.to_vec()),
        StoredValue::Int(i) => Owned::Int(i),
    })
}

fn from_owned(py: Python<'_>, value: Owned) -> PyResult<Py<PyAny>> {
    Ok(match value {
        Owned::Str(text) => PyString::new(py, &text).into_any().unbind(),
        Owned::Bytes(bytes) => PyBytes::new(py, &bytes).into_any().unbind(),
        Owned::Int(i) => i.into_pyobject(py)?.into_any().unbind(),
    })
}
