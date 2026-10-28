//! Phase 1, Spike 3: tagged JSON (serde_json and sonic-rs) and tagged msgpack codecs for Python
//! values. Throwaway; see plans/spike-3-codecs.md.

#![allow(clippy::needless_pass_by_value)]

mod common;
mod json;
mod mp;

use std::cell::RefCell;
use std::sync::atomic::Ordering;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use serde::de::DeserializeSeed;
use serde::ser::Serialize;

/// Scratch buffers above this are dropped after use rather than kept for the thread.
const SCRATCH_KEEP: usize = 1 << 20;

thread_local! {
    static SCRATCH: RefCell<Vec<u8>> = const { RefCell::new(Vec::new()) };
}

/// Runs `f` against a reused per-thread buffer (warm memory, inlined small writes), then copies the
/// result into one Python bytes object. Falls back to a fresh Vec if the buffer is already borrowed.
fn with_scratch<'py>(py: Python<'py>, f: impl FnOnce(&mut Vec<u8>) -> PyResult<()>) -> PyResult<Bound<'py, PyBytes>> {
    SCRATCH.with(|cell| match cell.try_borrow_mut() {
        Ok(mut buf) => {
            buf.clear();
            let r = f(&mut buf).map(|()| PyBytes::new(py, &buf));
            if buf.capacity() > SCRATCH_KEEP {
                *buf = Vec::new();
            }
            r
        }
        Err(_) => {
            let mut buf = Vec::new();
            f(&mut buf).map(|()| PyBytes::new(py, &buf))
        }
    })
}

fn json_into(py: Python<'_>, obj: &Bound<'_, PyAny>, out: &mut Vec<u8>, sonic: bool) -> PyResult<bool> {
    let ctx = json::Ctx::new(py)?;
    let enc = json::Enc { obj, ctx: &ctx, depth: 0 };
    let r = if sonic {
        enc.serialize(&mut sonic_rs::Serializer::new(&mut *out)).map_err(|e| e.to_string())
    } else {
        enc.serialize(&mut serde_json::Serializer::new(&mut *out)).map_err(|e| e.to_string())
    };
    r.map_err(|msg| ctx.take_err(msg))?;
    Ok(ctx.tagged.get())
}

/// Encodes to tagged JSON with serde_json; returns (bytes, whether any tag was written).
#[pyfunction]
fn json_encode_info<'py>(py: Python<'py>, obj: &Bound<'py, PyAny>) -> PyResult<(Bound<'py, PyBytes>, bool)> {
    let mut tagged = false;
    let b = with_scratch(py, |out| {
        tagged = json_into(py, obj, out, false)?;
        Ok(())
    })?;
    Ok((b, tagged))
}

#[pyfunction]
fn json_encode<'py>(py: Python<'py>, obj: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    with_scratch(py, |out| json_into(py, obj, out, false).map(|_| ()))
}

/// serde_json writing straight into the Python bytes object (no intermediate buffer), for comparison.
#[pyfunction]
fn json_encode_direct<'py>(py: Python<'py>, obj: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let ctx = json::Ctx::new(py)?;
    PyBytes::new_with_writer(py, 0, |w| {
        let enc = json::Enc { obj, ctx: &ctx, depth: 0 };
        enc.serialize(&mut serde_json::Serializer::new(w)).map_err(|e| ctx.take_err(e))
    })
}

#[pyfunction]
fn sonic_encode<'py>(py: Python<'py>, obj: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    with_scratch(py, |out| json_into(py, obj, out, true).map(|_| ()))
}

#[pyfunction]
fn json_decode<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let mut de = serde_json::Deserializer::from_slice(data);
    let v = json::Any { py }.deserialize(&mut de).map_err(|e| PyValueError::new_err(e.to_string()))?;
    de.end().map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(v)
}

#[pyfunction]
fn sonic_decode<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let mut de = sonic_rs::Deserializer::from_slice(data);
    let v = json::Any { py }.deserialize(&mut de).map_err(|e| PyValueError::new_err(e.to_string()))?;
    de.end().map_err(|e| PyValueError::new_err(e.to_string()))?;
    Ok(v)
}

#[pyfunction]
fn msgpack_encode<'py>(py: Python<'py>, obj: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let t = common::types(py)?;
    with_scratch(py, |out| mp::encode(obj, t, 0, out))
}

#[pyfunction]
fn msgpack_encode_direct<'py>(py: Python<'py>, obj: &Bound<'py, PyAny>) -> PyResult<Bound<'py, PyBytes>> {
    let t = common::types(py)?;
    PyBytes::new_with_writer(py, 0, |w| mp::encode(obj, t, 0, w))
}

#[pyfunction]
fn msgpack_decode<'py>(py: Python<'py>, data: &[u8]) -> PyResult<Bound<'py, PyAny>> {
    let mut r = mp::Reader::new(data);
    let v = r.value(py)?;
    if !r.finished() {
        return Err(PyValueError::new_err("trailing bytes after msgpack value"));
    }
    Ok(v)
}

/// Benchmark knob: turn the dict-key string cache on or off.
#[pyfunction]
fn set_key_cache(on: bool) {
    common::KEY_CACHE_ON.store(on, Ordering::Relaxed);
}

#[pymodule(gil_used = false)]
fn dco_codec_spike(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("_DEBUG_BUILD", cfg!(debug_assertions))?;
    m.add_function(wrap_pyfunction!(json_encode, m)?)?;
    m.add_function(wrap_pyfunction!(json_encode_direct, m)?)?;
    m.add_function(wrap_pyfunction!(json_encode_info, m)?)?;
    m.add_function(wrap_pyfunction!(json_decode, m)?)?;
    m.add_function(wrap_pyfunction!(sonic_encode, m)?)?;
    m.add_function(wrap_pyfunction!(sonic_decode, m)?)?;
    m.add_function(wrap_pyfunction!(msgpack_encode, m)?)?;
    m.add_function(wrap_pyfunction!(msgpack_encode_direct, m)?)?;
    m.add_function(wrap_pyfunction!(msgpack_decode, m)?)?;
    m.add_function(wrap_pyfunction!(set_key_cache, m)?)?;
    Ok(())
}
