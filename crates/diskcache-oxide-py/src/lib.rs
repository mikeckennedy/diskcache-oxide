//! The `diskcache_oxide._core` extension module: a thin PyO3 layer over `diskcache-oxide-core`.

// PyO3 extracts Python arguments into owned values, so taking them by value is the normal shape here.
#![allow(clippy::needless_pass_by_value)]

mod cache;
mod codec;

use std::cell::Cell;
use std::sync::atomic::{AtomicBool, Ordering};

use diskcache_oxide_core::{Error, gate, sqlite};
use pyo3::create_exception;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyRuntimeWarning, PyTimeoutError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::PyDict;

// Created under the public package name so instances pickle across multiprocessing as
// `diskcache_oxide.Timeout`; `diskcache_oxide/__init__.py` re-exports them.
create_exception!(
    diskcache_oxide,
    Timeout,
    PyTimeoutError,
    "The database stayed locked for longer than the cache's timeout."
);
create_exception!(
    diskcache_oxide,
    UnsafeValueError,
    PyTypeError,
    "A pickle=False cache read a value that was stored with pickle."
);
create_exception!(
    diskcache_oxide,
    FormatMismatchError,
    PyRuntimeError,
    "The cache directory holds a database in a format this version doesn't support."
);
create_exception!(
    diskcache_oxide,
    CorruptValueError,
    PyValueError,
    "A stored value couldn't be decoded: an unknown mode, or bytes that don't parse."
);
create_exception!(
    diskcache_oxide,
    ForeignValueError,
    CorruptValueError,
    "A stored value this build can't read but another might: an unknown mode, tag or extension (a \
     newer version wrote it), or a time zone missing from this host."
);

/// The SQLite version reported at runtime by `sqlite3_libversion()`.
#[pyfunction]
fn sqlite_version() -> &'static str {
    sqlite::runtime_version()
}

/// The compile-time options of the linked SQLite (`PRAGMA compile_options`).
#[pyfunction]
fn sqlite_compile_options() -> PyResult<Vec<String>> {
    sqlite::compile_options().map_err(to_py_err)
}

/// Maps a core error to the Python exception the public API documents.
pub(crate) fn to_py_err(error: Error) -> PyErr {
    let message = error.to_string();
    match error {
        Error::Timeout { .. } | Error::WriteLockBusyInTransaction => Timeout::new_err(message),
        Error::DiskFull { .. } => PyOSError::new_err((libc::ENOSPC, message)),
        Error::SqliteIo { errno: Some(errno), .. } => PyOSError::new_err((errno, message)),
        Error::Io { source, .. } => match source.raw_os_error() {
            Some(errno) => PyOSError::new_err((errno, message)),
            None => PyOSError::new_err(message),
        },
        Error::Unwritable
        | Error::ForkPoisoned
        | Error::NestedTransaction
        | Error::NoTransaction
        | Error::EndInsideOperation
        | Error::TransactionAborted => PyRuntimeError::new_err(message),
        Error::FormatMismatch { .. } => FormatMismatchError::new_err(message),
        Error::InvalidSetting(_) => PyValueError::new_err(message),
        Error::KeyMissing => pyo3::exceptions::PyKeyError::new_err(message),
        Error::NotNumeric => PyTypeError::new_err(message),
        Error::Overflow => pyo3::exceptions::PyOverflowError::new_err(message),
        _ => PyOSError::new_err(message),
    }
}

thread_local! {
    /// True while this thread runs inside [`detach`]: the interpreter is already released.
    static DETACHED: Cell<bool> = const { Cell::new(false) };
}

/// `py.detach(work)`, marking the thread as detached so the gate's blocker runs its waits directly.
/// For long Rust-only work (`clear`, `expire`, `cull`, opening a store, taking a held
/// transaction's locks): `work` must never attach the interpreter (no Python callbacks), or a
/// wait inside it would run attached.
pub(crate) fn detach<T, F>(py: Python<'_>, work: F) -> T
where
    F: Send + FnOnce() -> T,
    T: Send,
{
    py.detach(|| {
        struct Restore(bool);
        impl Drop for Restore {
            fn drop(&mut self) {
                DETACHED.with(|d| d.set(self.0));
            }
        }
        let _restore = Restore(DETACHED.with(|d| d.replace(true)));
        work()
    })
}

/// Runs a blocking fork-gate wait with the interpreter released, so the thread holding the gate
/// can finish (on GIL builds it needs the GIL) and a stop-the-world isn't held up by the waiter.
/// On a thread that's already detached it just waits: re-attaching only to detach again cost a
/// GIL build up to the 5 ms switch interval per wait while other threads kept the interpreter
/// busy (31 ms per 100-row `clear()` chunk with 2 writer and 2 reader threads; `bench.reattach`).
fn detached_wait(wait: &(dyn Fn() + Sync)) {
    if DETACHED.with(Cell::get) {
        wait();
    } else {
        Python::attach(|py| py.detach(wait));
    }
}

/// `os.register_at_fork(before=...)`: waits, with the interpreter released and no timeout, for every
/// other thread's SQLite work in this process to finish, and holds off new work until the fork.
#[pyfunction]
fn _before_fork(py: Python<'_>) {
    if gate::GATE.own_depth() > 0 {
        // CPython ignores exceptions from before-fork hooks, so the fork can't be refused; the
        // child sees the held transaction as poisoned. Ignore a warnings-as-errors failure too.
        let _ = PyErr::warn(
            py,
            &py.get_type::<PyRuntimeWarning>(),
            c"os.fork() called while this thread holds a diskcache_oxide transaction; it can't be used in the child",
            1,
        );
    }
    // Plain `py.detach`, not `crate::detach`: `begin_fork` sleep-polls itself and never calls the
    // blocker, so the detached flag would change nothing.
    py.detach(gate::begin_fork);
}

#[pyfunction]
fn _after_fork_in_parent() {
    gate::end_fork_parent();
}

#[pyfunction]
fn _after_fork_in_child() {
    gate::end_fork_child();
}

/// Set once the fork hooks are registered. The gate is process-wide, so its hooks must be too: a
/// second module init (a re-import after `_core` is removed from `sys.modules`) must not register a
/// second set, which would run every hook twice per fork. PyO3 refuses subinterpreters, so the one
/// interpreter that can fork is the one that registered them.
static FORK_HOOKS_REGISTERED: AtomicBool = AtomicBool::new(false);

/// Installs the fork barrier: the gate's blocker and the `os.register_at_fork` hooks. Registered
/// from the module itself so importing `_core` directly is enough. Idempotent.
fn install_fork_barrier(m: &Bound<'_, PyModule>) -> PyResult<()> {
    gate::set_blocker(detached_wait);
    if FORK_HOOKS_REGISTERED.swap(true, Ordering::AcqRel) {
        return Ok(());
    }
    let registered = register_fork_hooks(m);
    if registered.is_err() {
        // Let a later init try again.
        FORK_HOOKS_REGISTERED.store(false, Ordering::Release);
    }
    registered
}

fn register_fork_hooks(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    let os = py.import("os")?;
    if !os.hasattr("register_at_fork")? {
        return Ok(()); // No fork() on this platform.
    }
    let hooks = PyDict::new(py);
    hooks.set_item("before", wrap_pyfunction!(_before_fork, m)?)?;
    hooks.set_item("after_in_parent", wrap_pyfunction!(_after_fork_in_parent, m)?)?;
    hooks.set_item("after_in_child", wrap_pyfunction!(_after_fork_in_child, m)?)?;
    os.call_method("register_at_fork", (), Some(&hooks))?;
    Ok(())
}

/// Test hook: the bundled SQLite's (bytes in use now, high-water mark) across the whole process,
/// optionally resetting the mark. Not part of the public API.
#[pyfunction]
#[pyo3(signature = (reset = false))]
#[allow(unsafe_code)]
fn _sqlite_memory(reset: bool) -> (i64, i64) {
    // SAFETY: both functions only read (and optionally reset) SQLite's global memory counters;
    // they take no pointers and are safe to call from any thread at any time.
    unsafe { (rusqlite::ffi::sqlite3_memory_used(), rusqlite::ffi::sqlite3_memory_highwater(i32::from(reset))) }
}

#[pymodule(gil_used = false)]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add("BUNDLED_SQLITE_VERSION", sqlite::compile_version())?;
    // Lets benchmarks refuse to measure an unoptimized build.
    m.add("_DEBUG_BUILD", cfg!(debug_assertions))?;
    m.add("Timeout", py.get_type::<Timeout>())?;
    m.add("UnsafeValueError", py.get_type::<UnsafeValueError>())?;
    m.add("CorruptValueError", py.get_type::<CorruptValueError>())?;
    m.add("ForeignValueError", py.get_type::<ForeignValueError>())?;
    m.add_function(wrap_pyfunction!(sqlite_version, m)?)?;
    m.add_function(wrap_pyfunction!(sqlite_compile_options, m)?)?;
    // Test hooks for the value codec; not part of the public API.
    m.add_function(wrap_pyfunction!(codec::_encode, m)?)?;
    m.add_function(wrap_pyfunction!(codec::_decode, m)?)?;
    m.add_function(wrap_pyfunction!(_sqlite_memory, m)?)?;
    m.add("FormatMismatchError", py.get_type::<FormatMismatchError>())?;
    m.add_class::<cache::Cache>()?;
    install_fork_barrier(m)?;
    Ok(())
}
