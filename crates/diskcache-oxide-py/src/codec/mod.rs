//! Value codecs: one Python value to and from the `(mode, payload)` of a cache row (design section 5).
//!
//! Modes: 1 raw `str` (UTF-8), 2 raw `bytes`, 3 msgpack, 4 JSON, 5 pickle, 6 native int, 7 native
//! float. Only the exact types `str`, `bytes`, `int` (not `bool`) and `float` take the raw and
//! native modes; everything else goes through the structured serializer, whose tag format is
//! specified in plans/spike-3-codecs.md. Pickle is used only when the handle allows it *and* the
//! serializer can't represent the value.
//!
//! The encoder runs no user Python code of its own: it dispatches on exact types, rejects subclasses
//! and foreign tzinfo classes before calling anything on them, and calls only C-implemented
//! methods. Two things the caller opts into are the exceptions: the pickle fallback, and a
//! registered type (plan 16, [`registry`]), whose `encode` function or dataclass fields run inside
//! the encoder. `common::DictEntries` copies a dict's entries whenever anything is registered, so
//! that code can't change a dict under the encoder.

mod common;
mod json;
pub mod key;
mod msgpack;
pub mod registry;

use std::cell::RefCell;
use std::fmt;
use std::io::Write;

use pyo3::exceptions::{PyException, PyMemoryError, PyOSError, PyRecursionError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::sync::critical_section::with_critical_section;
use pyo3::types::{PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};
use pyo3::{PyTraverseError, PyVisit};

use registry::Snapshot;
pub use registry::TypeRegistry;

use crate::{CorruptValueError, ForeignValueError, UnsafeValueError};

/// The per-row `mode` column.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
#[repr(u8)]
pub enum Mode {
    RawStr = 1,
    RawBytes = 2,
    Msgpack = 3,
    Json = 4,
    Pickle = 5,
    Int = 6,
    Float = 7,
}

impl Mode {
    pub fn from_column(mode: i64) -> Option<Self> {
        Some(match mode {
            1 => Self::RawStr,
            2 => Self::RawBytes,
            3 => Self::Msgpack,
            4 => Self::Json,
            5 => Self::Pickle,
            6 => Self::Int,
            7 => Self::Float,
            _ => return None,
        })
    }

    /// The value stored in the `mode` column.
    pub fn as_u8(self) -> u8 {
        self as u8
    }
}

/// The structured serializer a handle writes with. Reads always decode by the row's mode.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Serializer {
    #[default]
    Msgpack,
    Json,
}

impl Serializer {
    /// Parses the `serializer=` argument: `'msgpack'` or `'json'`.
    pub fn from_name(name: &str) -> PyResult<Self> {
        match name {
            "msgpack" => Ok(Self::Msgpack),
            "json" => Ok(Self::Json),
            _ => Err(PyValueError::new_err(format!("serializer must be 'msgpack' or 'json', not {name:?}"))),
        }
    }

    fn mode(self) -> Mode {
        match self {
            Self::Msgpack => Mode::Msgpack,
            Self::Json => Mode::Json,
        }
    }
}

/// What gets bound into the `value` column. Raw `str` and `bytes` keep the caller's object, so
/// they're never copied before SQLite copies them.
#[derive(Debug)]
pub enum Payload<'py> {
    /// Mode 1. Already checked to be encodable as UTF-8.
    Str(Bound<'py, PyString>),
    /// Modes 2 to 5.
    Bytes(Bound<'py, PyBytes>),
    /// Mode 6, bound as SQLite INTEGER.
    Int(i64),
    /// Mode 7, bound as SQLite REAL (finite only).
    Float(f64),
}

impl Payload<'_> {
    /// The bytes to store for modes 1 to 5; `None` for the native modes.
    pub fn as_blob(&self) -> PyResult<Option<&[u8]>> {
        Ok(match self {
            Self::Str(s) => Some(s.to_str()?.as_bytes()),
            Self::Bytes(b) => Some(b.as_bytes()),
            Self::Int(_) | Self::Float(_) => None,
        })
    }
}

/// An encoded value: the row's mode and what goes in its `value` column.
#[derive(Debug)]
pub struct Encoded<'py> {
    pub mode: Mode,
    pub payload: Payload<'py>,
}

/// A row's `value` column as read back from SQLite.
#[derive(Clone, Copy, Debug)]
pub enum Stored<'a> {
    Blob(&'a [u8]),
    Int(i64),
    Float(f64),
}

impl fmt::Display for Stored<'_> {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            Self::Blob(_) => "BLOB",
            Self::Int(_) => "INTEGER",
            Self::Float(_) => "REAL",
        })
    }
}

/// The pickle protocol for mode-5 rows (diskcache's default, `pickle.HIGHEST_PROTOCOL` on 3.14).
/// If this is ever raised, an older build's "unsupported pickle protocol" error must become
/// `ForeignValueError` (a newer peer's data), not a corrupt row that `on_decode_error` deletes.
const PICKLE_PROTOCOL: u8 = 5;

/// A handle's serialization settings: the structured serializer, whether pickle may be used, and
/// the registry consulted before the global one (`types=`). Built once per handle; every encode and
/// decode goes through it.
#[derive(Debug)]
pub struct Codec {
    pub serializer: Serializer,
    pub pickle: bool,
    pub types: Option<Py<TypeRegistry>>,
}

impl Codec {
    /// Whether a row of `mode` must be refused without reading its value (see [`refuses`]).
    #[must_use]
    pub fn refuses(&self, mode: i64) -> bool {
        refuses(mode, self.pickle)
    }

    /// Visits the registry this handle holds, for `__traverse__`.
    pub fn traverse(&self, visit: &PyVisit<'_>) -> Result<(), PyTraverseError> {
        if let Some(types) = &self.types {
            visit.call(types)?;
        }
        Ok(())
    }

    fn snapshot(&self, py: Python<'_>) -> PyResult<Snapshot> {
        Snapshot::take(py, self.types.as_ref())
    }

    /// Encodes `value` for storage. Raises `TypeError` for a value the serializer can't represent,
    /// unless `pickle` is true, in which case that value is pickled instead. A value that reached a
    /// registered type is never pickled: whatever it raises, raises.
    pub fn encode<'py>(&self, py: Python<'py>, value: &Bound<'py, PyAny>) -> PyResult<Encoded<'py>> {
        if let Ok(s) = value.cast_exact::<PyString>() {
            s.to_str()?; // Lone surrogates can't be stored as UTF-8: raise now, not at bind time.
            return Ok(Encoded { mode: Mode::RawStr, payload: Payload::Str(s.clone()) });
        }
        if let Ok(b) = value.cast_exact::<PyBytes>() {
            return Ok(Encoded { mode: Mode::RawBytes, payload: Payload::Bytes(b.clone()) });
        }
        // Exact int only, so bool (an int subclass) and IntEnum never come back as plain ints.
        if value.is_exact_instance_of::<PyInt>()
            && let Ok(i) = value.extract::<i64>()
        {
            return Ok(Encoded { mode: Mode::Int, payload: Payload::Int(i) });
        }
        // NaN and the infinities stay in the serializer: SQLite binds NaN as NULL (D-008). -0.0 is
        // native: it round-trips through a BLOB-affinity column (tests::negative_zero_survives_real).
        if let Ok(f) = value.cast_exact::<PyFloat>()
            && f.value().is_finite()
        {
            return Ok(Encoded { mode: Mode::Float, payload: Payload::Float(f.value()) });
        }
        let reg = self.snapshot(py)?;
        match serialize(py, value, self.serializer, &reg) {
            Ok(bytes) => Ok(Encoded { mode: self.serializer.mode(), payload: Payload::Bytes(bytes) }),
            Err(e) if self.pickle && !reg.had_hit() && e.is_instance_of::<PyTypeError>(py) => {
                let dumped = common::types(py)?.pickle_dumps.bind(py).call1((value, PICKLE_PROTOCOL))?;
                Ok(Encoded { mode: Mode::Pickle, payload: Payload::Bytes(dumped.cast_into::<PyBytes>()?) })
            }
            Err(e) => Err(e),
        }
    }

    /// Decodes a row's value. A mode-5 row raises `UnsafeValueError` unless `pickle`; a row that
    /// can't be decoded (unknown mode, wrong storage class, malformed bytes) raises `CorruptValueError`.
    pub fn decode(&self, py: Python<'_>, mode: i64, stored: Stored<'_>) -> PyResult<Py<PyAny>> {
        let Some(mode) = Mode::from_column(mode) else {
            return Err(ForeignValueError::new_err(format!("unknown value mode {mode}")));
        };
        if mode == Mode::Pickle && !self.pickle {
            return Err(pickle_refused());
        }
        let value = match (mode, stored) {
            (Mode::RawStr, Stored::Blob(b)) => match std::str::from_utf8(b) {
                Ok(s) => PyString::new(py, s).into_any(),
                Err(e) => return Err(CorruptValueError::new_err(format!("a str row holds invalid UTF-8: {e}"))),
            },
            (Mode::RawBytes, Stored::Blob(b)) => PyBytes::new(py, b).into_any(),
            (Mode::Msgpack, Stored::Blob(b)) => {
                let reg = self.snapshot(py)?;
                msgpack::decode(py, b, &reg).map_err(|e| as_corrupt(py, e, "msgpack"))?
            }
            (Mode::Json, Stored::Blob(b)) => {
                let reg = self.snapshot(py)?;
                json::decode(py, b, &reg).map_err(|e| match e {
                    json::DecodeError::Python(e) => as_corrupt(py, e, "JSON"),
                    json::DecodeError::Invalid(msg) => {
                        CorruptValueError::new_err(format!("undecodable JSON value: {msg}"))
                    }
                })?
            }
            (Mode::Pickle, Stored::Blob(b)) => {
                // Errors from unpickling (a missing class, a failing __setstate__) are the user's
                // own and pass through unchanged, as they do with diskcache.
                common::types(py)?.pickle_loads.bind(py).call1((PyBytes::new(py, b),))?
            }
            (Mode::Int, Stored::Int(i)) => i.into_pyobject(py)?.into_any(),
            (Mode::Float, Stored::Float(f)) => PyFloat::new(py, f).into_any(),
            (mode, stored) => {
                return Err(CorruptValueError::new_err(format!("a mode-{} row holds a {stored} value", mode.as_u8())));
            }
        };
        Ok(value.unbind())
    }
}

/// Whether a handle with this `allow_pickle` must refuse a row of `mode` without reading its value:
/// a pickle it may not run (D-001), or a mode this build doesn't know (a newer peer's data, which
/// it must not destroy either).
#[must_use]
pub fn refuses(mode: i64, allow_pickle: bool) -> bool {
    match Mode::from_column(mode) {
        Some(Mode::Pickle) => !allow_pickle,
        Some(_) => false,
        None => true,
    }
}

/// Whether `mode` is the pickle mode (errors from unpickling are the user's own code's).
#[must_use]
pub fn is_pickle(mode: i64) -> bool {
    Mode::from_column(mode) == Some(Mode::Pickle)
}

/// The error for a row [`refuses`] turned away, without its value.
pub fn refusal(mode: i64) -> PyErr {
    match Mode::from_column(mode) {
        Some(_) => pickle_refused(),
        None => ForeignValueError::new_err(format!("unknown value mode {mode}")),
    }
}

/// The error for a pickled row read by a `pickle=False` handle (D-001).
pub fn pickle_refused() -> PyErr {
    UnsafeValueError::new_err("this value was stored with pickle; open the cache with pickle=True to read it")
}

/// Imports what the codecs use, on the caller's stack. Otherwise it happens on whichever thread first
/// encodes a container, and an import can need more stack than a small worker thread has.
pub fn load_types(py: Python<'_>) -> PyResult<()> {
    common::types(py).map(|_| ())
}

/// Wraps a Python error raised while decoding as `CorruptValueError`, keeping it as `__cause__`.
/// `MemoryError`, `RecursionError` (this thread's stack, not the row: another thread can read it),
/// `OSError` (`Timeout` included) and `UnsafeValueError` from a registered type's decode function,
/// and non-`Exception` errors (`KeyboardInterrupt`) pass through unchanged.
fn as_corrupt(py: Python<'_>, e: PyErr, what: &str) -> PyErr {
    if passes_through(py, &e) {
        return e;
    }
    let corrupt = CorruptValueError::new_err(format!("undecodable {what} value: {e}"));
    corrupt.set_cause(py, Some(e));
    corrupt
}

/// Whether an error raised while decoding is about something other than the stored bytes, so it is
/// returned as itself rather than wrapped as `CorruptValueError`: `CorruptValueError` already (a
/// nested read's, or `ForeignValueError`), `MemoryError`, `RecursionError` (this thread's stack),
/// `OSError` (`Timeout` included) and `UnsafeValueError` from a pickle hook or a registered type's
/// decode function, and anything that isn't an `Exception` (`KeyboardInterrupt`). One list, used by
/// every decoder, so an exemption can't be added to one and not another.
pub(super) fn passes_through(py: Python<'_>, e: &PyErr) -> bool {
    e.is_instance_of::<CorruptValueError>(py)
        || e.is_instance_of::<PyMemoryError>(py)
        || e.is_instance_of::<PyRecursionError>(py)
        || e.is_instance_of::<PyOSError>(py)
        || e.is_instance_of::<UnsafeValueError>(py)
        || !e.is_instance_of::<PyException>(py)
}

// ---- output buffers ----

/// Values whose large strings or bytes add up to more than this are written straight into the
/// Python bytes object; smaller ones go through the per-thread scratch buffer (Spike 3 measured
/// each to be faster on its side of the line).
const DIRECT_WRITE_THRESHOLD: usize = 64 * 1024;
/// How many items of a top-level list or tuple the size pre-scan looks at.
const PRESCAN_ITEMS: usize = 64;
/// Scratch buffers above this are dropped after use rather than kept for the thread.
const SCRATCH_KEEP: usize = 1 << 20;

thread_local! {
    static SCRATCH: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

fn serialize<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    serializer: Serializer,
    reg: &Snapshot,
) -> PyResult<Bound<'py, PyBytes>> {
    let estimate = large_size_estimate(value);
    if estimate > DIRECT_WRITE_THRESHOLD {
        // JSON writes bytes as base64, a third bigger.
        let reserve = match serializer {
            Serializer::Msgpack => estimate,
            Serializer::Json => estimate / 3 * 4,
        } + 1024;
        return PyBytes::new_with_writer(py, reserve, |w| write_value(py, value, serializer, reg, w));
    }
    SCRATCH.with(|cell| {
        let Ok(mut buf) = cell.try_borrow_mut() else {
            // Already in use further up this thread's stack: use a fresh buffer.
            let mut buf = Vec::new();
            return write_value(py, value, serializer, reg, &mut buf).map(|()| PyBytes::new(py, &buf));
        };
        buf.clear();
        let r = write_value(py, value, serializer, reg, &mut *buf).map(|()| PyBytes::new(py, &buf));
        if buf.capacity() > SCRATCH_KEEP {
            *buf = Vec::new();
        }
        r
    })
}

fn write_value<W: Write>(
    py: Python<'_>,
    value: &Bound<'_, PyAny>,
    serializer: Serializer,
    reg: &Snapshot,
    out: &mut W,
) -> PyResult<()> {
    let t = common::types(py)?;
    match serializer {
        Serializer::Msgpack => msgpack::encode(value, &msgpack::EncodeCtx { t, reg }, 0, out),
        Serializer::Json => json::encode(py, value, reg, out),
    }
}

/// A cheap look for big strings and bytes: the value itself, or the direct children of a top-level
/// container (every dict value, the first `PRESCAN_ITEMS` list or tuple items). Returns their total
/// length, or 0 if none is over the threshold on its own.
fn large_size_estimate(value: &Bound<'_, PyAny>) -> usize {
    fn leaf_len(o: &Bound<'_, PyAny>) -> usize {
        if o.is_exact_instance_of::<PyBytes>() || o.is_exact_instance_of::<PyString>() {
            o.len().unwrap_or(0)
        } else {
            0
        }
    }
    let total = if let Ok(d) = value.cast_exact::<PyDict>() {
        // PyO3's dict iterator panics if the dict resizes after the iterator is created, so on
        // the free-threaded build hold the dict's lock from before `iter()` to the end of the
        // scan (a no-op with the GIL). leaf_len takes no other lock, so the section is never
        // suspended mid-scan.
        with_critical_section(d, || d.iter().fold(0, |acc, (_, v)| acc + leaf_len(&v)))
    } else if let Ok(l) = value.cast_exact::<PyList>() {
        (0..l.len().min(PRESCAN_ITEMS)).filter_map(|i| l.get_item(i).ok()).map(|v| leaf_len(&v)).sum()
    } else if let Ok(t) = value.cast_exact::<PyTuple>() {
        t.iter().take(PRESCAN_ITEMS).map(|v| leaf_len(&v)).sum()
    } else {
        leaf_len(value)
    };
    if total > DIRECT_WRITE_THRESHOLD { total } else { 0 }
}

// ---- test hooks (private module functions) ----

/// Test hook: `_core._encode(value, serializer='msgpack', pickle=False, types=None) -> (mode, payload)`.
#[pyfunction]
#[pyo3(signature = (value, serializer = "msgpack", pickle = false, types = None))]
pub fn _encode<'py>(
    py: Python<'py>,
    value: &Bound<'py, PyAny>,
    serializer: &str,
    pickle: bool,
    types: Option<Py<TypeRegistry>>,
) -> PyResult<(u8, Bound<'py, PyAny>)> {
    let codec = Codec { serializer: Serializer::from_name(serializer)?, pickle, types };
    let encoded = codec.encode(py, value)?;
    let payload = match &encoded.payload {
        Payload::Str(_) => PyBytes::new(py, encoded.payload.as_blob()?.unwrap_or_default()).into_any(),
        Payload::Bytes(b) => b.clone().into_any(),
        Payload::Int(i) => i.into_pyobject(py)?.into_any(),
        Payload::Float(f) => PyFloat::new(py, *f).into_any(),
    };
    Ok((encoded.mode.as_u8(), payload))
}

/// Test hook: `_core._decode(mode, payload, allow_pickle=False, types=None)`, with the payload a
/// `bytes`, `int` or `float` standing for a BLOB, INTEGER or REAL column.
#[pyfunction]
#[pyo3(signature = (mode, payload, allow_pickle = false, types = None))]
pub fn _decode(
    py: Python<'_>,
    mode: i64,
    payload: &Bound<'_, PyAny>,
    allow_pickle: bool,
    types: Option<Py<TypeRegistry>>,
) -> PyResult<Py<PyAny>> {
    let codec = Codec { serializer: Serializer::Msgpack, pickle: allow_pickle, types };
    if let Ok(b) = payload.cast_exact::<PyBytes>() {
        codec.decode(py, mode, Stored::Blob(b.as_bytes()))
    } else if payload.is_exact_instance_of::<PyInt>() {
        codec.decode(py, mode, Stored::Int(payload.extract()?))
    } else if let Ok(f) = payload.cast_exact::<PyFloat>() {
        codec.decode(py, mode, Stored::Float(f.value()))
    } else {
        Err(PyTypeError::new_err("payload must be bytes, int or float"))
    }
}

#[cfg(test)]
mod tests {
    use diskcache_oxide_core::sqlite;

    /// Design section 5: mode 7 stores -0.0 natively only if SQLite keeps its sign. The `value`
    /// column has BLOB affinity, so SQLite must not turn an integral REAL into an INTEGER.
    #[test]
    fn negative_zero_survives_real() {
        let conn = sqlite::open_in_memory().unwrap();
        conn.execute_batch("CREATE TABLE cache (mode INTEGER NOT NULL, value BLOB)").unwrap();
        for v in [-0.0_f64, 0.0, f64::MIN_POSITIVE / 4.0, -f64::MAX, 1e300, -1.5] {
            conn.execute("DELETE FROM cache", []).unwrap();
            conn.execute("INSERT INTO cache VALUES (7, ?1)", [v]).unwrap();
            let (kind, back): (String, f64) =
                conn.query_row("SELECT typeof(value), value FROM cache", [], |r| Ok((r.get(0)?, r.get(1)?))).unwrap();
            assert_eq!(kind, "real", "{v:?} stored as {kind}");
            assert_eq!(back.to_bits(), v.to_bits(), "{v:?} came back as {back:?}");
        }
    }

    #[test]
    fn i64_bounds_survive_integer() {
        let conn = sqlite::open_in_memory().unwrap();
        for v in [i64::MIN, -1, 0, i64::MAX] {
            let back: i64 = conn.query_row("SELECT ?1", [v], |r| r.get(0)).unwrap();
            assert_eq!(back, v);
        }
    }
}
