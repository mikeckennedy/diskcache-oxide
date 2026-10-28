//! Shared pieces: cached Python types, the dict-key string cache, tag names, and the helpers both
//! formats use for the types that are encoded through their Python string form.

use std::cell::RefCell;
use std::sync::atomic::{AtomicBool, Ordering};

use pyo3::exceptions::{PyTypeError, PyValueError};
use pyo3::intern;
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyString, PyTuple, PyType};

/// Nesting limit, counted in output containers (a tag wrapper counts as one). serde_json's decoder
/// stops at 128, so the encoder refuses anything deeper rather than write a row it can't read back.
pub const MAX_DEPTH: u32 = 120;

// Tag names. A tag is a map with exactly one entry whose key is one of these. Any user dict whose
// first key starts with '$' is wrapped in TAG_ESC, so the whole '$' namespace is reserved.
pub const TAG_BYTES: &str = "$bytes";
pub const TAG_TUPLE: &str = "$tuple";
pub const TAG_DICT: &str = "$dict";
pub const TAG_FLOAT: &str = "$float";
pub const TAG_INT: &str = "$int";
pub const TAG_DATETIME: &str = "$datetime";
pub const TAG_DATE: &str = "$date";
pub const TAG_DECIMAL: &str = "$decimal";
pub const TAG_SET: &str = "$set";
pub const TAG_FROZENSET: &str = "$frozenset";
pub const TAG_ESC: &str = "$esc";

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Tag {
    Bytes,
    Tuple,
    Dict,
    Float,
    Int,
    Datetime,
    Date,
    Decimal,
    Set,
    FrozenSet,
    Esc,
}

impl Tag {
    pub fn parse(key: &str) -> Option<Self> {
        Some(match key {
            TAG_BYTES => Self::Bytes,
            TAG_TUPLE => Self::Tuple,
            TAG_DICT => Self::Dict,
            TAG_FLOAT => Self::Float,
            TAG_INT => Self::Int,
            TAG_DATETIME => Self::Datetime,
            TAG_DATE => Self::Date,
            TAG_DECIMAL => Self::Decimal,
            TAG_SET => Self::Set,
            TAG_FROZENSET => Self::FrozenSet,
            TAG_ESC => Self::Esc,
            _ => return None,
        })
    }
}

/// Python objects looked up once per interpreter.
pub struct Types {
    pub datetime: Py<PyType>,
    pub date: Py<PyType>,
    pub timezone: Py<PyType>,
    pub utc: Py<PyAny>,
    pub zoneinfo: Py<PyType>,
    pub decimal: Py<PyType>,
    pub dt_fromisoformat: Py<PyAny>,
    pub date_fromisoformat: Py<PyAny>,
}

static TYPES: PyOnceLock<Types> = PyOnceLock::new();

pub fn types(py: Python<'_>) -> PyResult<&'static Types> {
    TYPES.get_or_try_init(py, || {
        let dtm = py.import("datetime")?;
        let datetime = dtm.getattr("datetime")?.cast_into::<PyType>()?;
        let date = dtm.getattr("date")?.cast_into::<PyType>()?;
        Ok(Types {
            dt_fromisoformat: datetime.getattr("fromisoformat")?.unbind(),
            date_fromisoformat: date.getattr("fromisoformat")?.unbind(),
            datetime: datetime.unbind(),
            date: date.unbind(),
            timezone: dtm.getattr("timezone")?.cast_into::<PyType>()?.unbind(),
            utc: dtm.getattr("timezone")?.getattr("utc")?.unbind(),
            zoneinfo: py.import("zoneinfo")?.getattr("ZoneInfo")?.cast_into::<PyType>()?.unbind(),
            decimal: py.import("decimal")?.getattr("Decimal")?.cast_into::<PyType>()?.unbind(),
        })
    })
}

// ---- dict-key cache ----

pub static KEY_CACHE_ON: AtomicBool = AtomicBool::new(true);
const KEY_CACHE_SLOTS: usize = 1024;
const KEY_CACHE_MAX_LEN: usize = 64;

thread_local! {
    // Direct-mapped, per thread, so there's no lock on the free-threaded build.
    static KEYS: RefCell<Vec<Option<Py<PyString>>>> = RefCell::new((0..KEY_CACHE_SLOTS).map(|_| None).collect());
}

fn fnv1a(bytes: &[u8]) -> u64 {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for &b in bytes {
        h ^= u64::from(b);
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    h
}

/// A Python str for a dict key, from the per-thread cache when the key is short.
pub fn key_str<'py>(py: Python<'py>, s: &str) -> Bound<'py, PyString> {
    if s.len() > KEY_CACHE_MAX_LEN || !KEY_CACHE_ON.load(Ordering::Relaxed) {
        return PyString::new(py, s);
    }
    #[allow(clippy::cast_possible_truncation)]
    let slot = (fnv1a(s.as_bytes()) as usize) & (KEY_CACHE_SLOTS - 1);
    KEYS.with(|cell| {
        let mut slots = cell.borrow_mut();
        if let Some(cached) = &slots[slot] {
            let b = cached.bind(py);
            if b.to_str().is_ok_and(|c| c == s) {
                return b.clone();
            }
        }
        let fresh = PyString::new(py, s);
        slots[slot] = Some(fresh.clone().unbind());
        fresh
    })
}

// ---- datetime / date / decimal through their string forms ----

/// The tag text for an exact `datetime`, or TypeError for one whose tzinfo can't round-trip.
pub fn datetime_text(o: &Bound<'_, PyAny>, t: &Types) -> PyResult<String> {
    let py = o.py();
    let tz = o.getattr(intern!(py, "tzinfo"))?;
    let fold: u8 = o.getattr(intern!(py, "fold"))?.extract()?;
    let mut iso: String = o.call_method0(intern!(py, "isoformat"))?.extract()?;
    let fold_suffix = if fold == 0 { "" } else { FOLD_SUFFIX };
    if tz.is_none() || tz.is(t.utc.bind(py)) {
        iso.push_str(fold_suffix);
        return Ok(iso);
    }
    let tz_type = tz.get_type();
    if tz_type.is(t.timezone.bind(py)) {
        iso.push_str(fold_suffix);
        // A timezone built with a custom name would come back unnamed.
        let offset = tz.call_method1(intern!(py, "utcoffset"), (py.None(),))?;
        let plain = t.timezone.bind(py).call1((offset,))?;
        if !tz.call_method1(intern!(py, "tzname"), (py.None(),))?.eq(plain.call_method1(intern!(py, "tzname"), (py.None(),))?)? {
            return Err(PyTypeError::new_err("a datetime.timezone with a custom name can't round-trip"));
        }
        return Ok(iso);
    }
    if tz_type.is(t.zoneinfo.bind(py)) {
        let key = tz.getattr(intern!(py, "key"))?;
        if key.is_none() {
            return Err(PyTypeError::new_err("a ZoneInfo without a key (from_file) can't round-trip"));
        }
        let key: String = key.extract()?;
        // RFC 9557 (IXDTF) zone suffix; the offset stays in the text as a check against tzdata drift.
        return Ok(format!("{iso}[{key}]{fold_suffix}"));
    }
    Err(PyTypeError::new_err(format!("tzinfo of type {} can't round-trip; use datetime.timezone or zoneinfo.ZoneInfo", tz_type.name()?)))
}

/// An RFC 9557 experimental (underscore) suffix key: present only when `fold == 1`.
const FOLD_SUFFIX: &str = "[_fold=1]";

pub fn datetime_from_text<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyAny>> {
    let t = types(py)?;
    let (s, fold) = match s.strip_suffix(FOLD_SUFFIX) {
        Some(rest) => (rest, true),
        None => (s, false),
    };
    let zone_split = s.strip_suffix(']').and_then(|r| r.rfind('[').map(|i| (&r[..i], &r[i + 1..])));
    let (iso, zone) = match zone_split {
        Some((iso, key)) => (iso, Some(key)),
        None => (s, None),
    };
    let parsed = t.dt_fromisoformat.bind(py).call1((iso,))?;
    if zone.is_none() && !fold {
        return Ok(parsed);
    }
    let kwargs = pyo3::types::PyDict::new(py);
    if fold {
        kwargs.set_item("fold", 1)?;
    }
    if let Some(key) = zone {
        // The wall time, zone and fold are authoritative. If tzdata changed since the write, the
        // offset differs from the stored one; the zone's current rules win.
        kwargs.set_item("tzinfo", t.zoneinfo.bind(py).call1((key,))?)?;
    }
    parsed.call_method("replace", (), Some(&kwargs))
}

pub fn date_from_text<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyAny>> {
    types(py)?.date_fromisoformat.bind(py).call1((s,))
}

pub fn decimal_from_text<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyAny>> {
    types(py)?.decimal.bind(py).call1((s,))
}

pub fn float_from_text<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyAny>> {
    let f = match s {
        "nan" => f64::NAN,
        "inf" => f64::INFINITY,
        "-inf" => f64::NEG_INFINITY,
        _ => return Err(PyValueError::new_err(format!("bad $float payload {s:?}"))),
    };
    Ok(pyo3::types::PyFloat::new(py, f).into_any())
}

pub fn nonfinite_text(f: f64) -> &'static str {
    if f.is_nan() {
        "nan"
    } else if f > 0.0 {
        "inf"
    } else {
        "-inf"
    }
}

pub fn int_from_text<'py>(py: Python<'py>, s: &str) -> PyResult<Bound<'py, PyAny>> {
    py.get_type::<pyo3::types::PyInt>().call1((s,))
}

pub fn unsupported(o: &Bound<'_, PyAny>) -> PyErr {
    let name = o.get_type().fully_qualified_name().map_or_else(|_| "?".to_owned(), |n| n.to_string());
    PyTypeError::new_err(format!("values of type {name} can't be stored without pickle (only exact built-in types round-trip)"))
}

pub fn tuple_from<'py>(py: Python<'py>, items: Vec<Bound<'py, PyAny>>) -> PyResult<Bound<'py, PyAny>> {
    Ok(PyTuple::new(py, items)?.into_any())
}
