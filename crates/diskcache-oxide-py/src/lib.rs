//! The `diskcache_oxide._core` extension module: a thin PyO3 layer over `diskcache-oxide-core`.

// PyO3 extracts Python arguments into owned values, so taking them by value is the normal shape here.
#![allow(clippy::needless_pass_by_value)]

mod cache;
mod codec;
mod durable;

use std::cell::Cell;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;

use diskcache_oxide_core::sqlite_mutex::{self, MutexStatus};
use diskcache_oxide_core::{Error, gate, sqlite, store};
use pyo3::create_exception;
use pyo3::exceptions::{
    PyImportError, PyMemoryError, PyOSError, PyRuntimeError, PyRuntimeWarning, PyTimeoutError, PyTypeError,
    PyValueError,
};
use pyo3::prelude::*;
use pyo3::sync::PyOnceLock;
use pyo3::types::{PyDict, PyType};

// Created under the public package name so instances pickle across multiprocessing as
// `diskcache_oxide.Timeout`; `diskcache_oxide/__init__.py` re-exports them.
create_exception!(
    diskcache_oxide,
    Timeout,
    PyTimeoutError,
    "A lock stayed held, or a fork stayed pending, for longer than the cache's timeout."
);
create_exception!(
    diskcache_oxide,
    TransactionBusy,
    Timeout,
    "A thread holding one cache's transaction couldn't begin another's because another thread holds its write lock."
);
create_exception!(
    diskcache_oxide,
    TransactionAborted,
    PyRuntimeError,
    "A held transaction didn't commit and can't: it was rolled back (after an error inside it, or by another \
     thread or an earlier exit), so nothing written in it will be committed."
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

/// `LayoutMismatchError(FormatMismatchError, ValueError)`: a directory whose layout doesn't match
/// how it was opened. `create_exception!` takes one base, so the class is built with `type()`.
static LAYOUT_MISMATCH: PyOnceLock<Py<PyType>> = PyOnceLock::new();

fn layout_mismatch_type(py: Python<'_>) -> PyResult<&Bound<'_, PyType>> {
    let class = LAYOUT_MISMATCH.get_or_try_init(py, || -> PyResult<Py<PyType>> {
        let bases = (py.get_type::<FormatMismatchError>(), py.get_type::<PyValueError>());
        let namespace = PyDict::new(py);
        namespace.set_item("__module__", "diskcache_oxide")?;
        namespace.set_item(
            "__doc__",
            "The directory's layout doesn't match how it was opened: a plain Cache on a FanoutCache root or \
             shard, a FanoutCache on a plain Cache's directory, or shards= that differs from the stored count. \
             Also a ValueError.",
        )?;
        let class = py.get_type::<PyType>().call1(("LayoutMismatchError", bases, namespace))?;
        Ok(class.cast_into::<PyType>()?.unbind())
    })?;
    Ok(class.bind(py))
}

fn layout_mismatch(message: String) -> PyErr {
    Python::attach(|py| match layout_mismatch_type(py) {
        Ok(class) => PyErr::from_type(class.clone(), message),
        Err(err) => err,
    })
}

/// Maps a core error to the Python exception the public API documents.
pub(crate) fn to_py_err(error: Error) -> PyErr {
    let message = error.to_string();
    if error.is_out_of_memory() {
        return PyMemoryError::new_err(message);
    }
    match error {
        Error::Timeout { .. } => Timeout::new_err(message),
        Error::WriteLockBusyInTransaction => TransactionBusy::new_err(message),
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
        | Error::EndInsideOperation => PyRuntimeError::new_err(message),
        Error::TransactionAborted => TransactionAborted::new_err(message),
        Error::FormatMismatch { .. } | Error::ManifestInvalid { .. } => FormatMismatchError::new_err(message),
        Error::RoleMismatch { .. } | Error::ShardCountMismatch { .. } | Error::StoredShardsRefuseSettings { .. } => {
            layout_mismatch(message)
        }
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
///
/// No timeout because Python can't refuse a fork, and proceeding without quiescence would hand the
/// child half a transaction. So code that holds a transaction or runs in a pickle hook, and depends
/// on another thread's cache call, can keep this waiting: that other call gets `Timeout` at its
/// `timeout`, and the fork proceeds once the first thread lets go.
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
fn _after_fork_in_parent(py: Python<'_>) {
    gate::end_fork_parent();
    drain_deferred_releases(py);
}

#[pyfunction]
fn _after_fork_in_child(py: Python<'_>) {
    gate::end_fork_child();
    drain_deferred_releases(py);
}

/// Releases the handles closed behind the fork that just ended. One atomic load unless there are
/// some; closing a last connection can run SQLite's own checkpoint, so that's detached.
fn drain_deferred_releases(py: Python<'_>) {
    if gate::GATE.has_deferred() {
        detach(py, store::drain_deferred_releases);
    }
}

/// Test hook: the fork gate's state and counters. Not part of the public API.
#[pyfunction]
fn _gate_stats(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let s = gate::GATE.stats();
    let d = PyDict::new(py);
    d.set_item("pending", s.pending)?;
    d.set_item("holders", s.holders)?;
    d.set_item("parked", s.parked)?;
    d.set_item("fork_timeouts", s.fork_timeouts)?;
    d.set_item("deferred", s.deferred)?;
    Ok(d)
}

/// Test hook: how many stores in this process have a held transaction (the count `FanoutCache`'s
/// other-shard check reads first). Not part of the public API.
#[pyfunction]
fn _held_stores() -> usize {
    store::Store::held_stores_in_process()
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
    // Before-hooks run in reverse registration order, and these two take a module lock in theirs and
    // keep it until the after-hook. Imported first, they run after ours, so those locks are only
    // taken once the fork has quiesced: a pickle hook that logs or submits work while a fork waits for
    // it can still finish and let go. A failed import mustn't stop the hooks registering.
    let _ = py.import("logging");
    let _ = py.import("concurrent.futures.thread");
    let hooks = PyDict::new(py);
    hooks.set_item("before", wrap_pyfunction!(_before_fork, m)?)?;
    hooks.set_item("after_in_parent", wrap_pyfunction!(_after_fork_in_parent, m)?)?;
    hooks.set_item("after_in_child", wrap_pyfunction!(_after_fork_in_child, m)?)?;
    os.call_method("register_at_fork", (), Some(&hooks))?;
    Ok(())
}

/// Test hook: the exception a SQLite result code becomes, as a store call would raise it.
#[pyfunction]
fn _sqlite_error(code: i32) -> PyErr {
    let failure = rusqlite::Error::SqliteFailure(rusqlite::ffi::Error::new(code), None);
    to_py_err(Error::from_sqlite(failure, Duration::ZERO))
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

/// Coverage builds only (`scripts/coverage.sh`): writes this process's coverage profile now. A
/// forked child that leaves through `os._exit` skips the exit handler that would write it, so the
/// script's `sitecustomize` calls this first.
#[cfg(coverage)]
#[pyfunction]
#[allow(unsafe_code)]
fn _write_coverage() -> i32 {
    unsafe extern "C" {
        fn __llvm_profile_write_file() -> std::ffi::c_int;
    }
    // SAFETY: `-C instrument-coverage` links LLVM's profile runtime, which defines this function. It
    // takes no arguments and writes the counters to the file `LLVM_PROFILE_FILE` names.
    unsafe { __llvm_profile_write_file() }
}

/// Test hook: whether SQLite runs on this crate's mutex methods, as `MutexStatus::as_str` names it
/// (`"installed"`, `"too_late"`, `"raced"` or `"init_failed"`). Live: `init_failed` becomes
/// `installed` once a later initialization ran them. Not part of the public API.
#[pyfunction]
fn _sqlite_mutex_status() -> &'static str {
    sqlite_mutex::status().map_or("not_attempted", MutexStatus::as_str)
}

/// Installs SQLite's mutex methods before anything else in the module can reach SQLite (plan 12
/// step 5a): the module init is the earliest point, so no other thread can be opening a cache.
///
/// - `Raced` refuses the import: SQLite was initialized concurrently with the install, and two mutex
///   implementations now coexist, which ends in heap corruption.
/// - `TooLate` (SQLite was initialized before, outside this crate) is safe but unspun, and is logged
///   once, as a warning on the `diskcache_oxide` logger. Nothing in these crates can cause it.
/// - `InitFailed` (`sqlite3_initialize` failed, in practice out of memory) is logged and the import
///   goes on. It's sound: our methods stay configured, every open retries the initialization with
///   them, and an open that still can't initialize fails with the SQLite error at that call.
fn install_sqlite_mutex(py: Python<'_>) -> PyResult<()> {
    let status = sqlite_mutex::ensure_installed();
    let message = match status {
        MutexStatus::Installed => return Ok(()),
        MutexStatus::Raced => {
            return Err(PyImportError::new_err(
                "diskcache_oxide: SQLite was initialized concurrently with the install of its mutex \
                 methods, so this process's SQLite state is unsound; refusing to load",
            ));
        }
        MutexStatus::TooLate => {
            "SQLite was already initialized when diskcache_oxide loaded, so it runs on SQLite's own \
             mutexes (correct, without the spin)"
        }
        MutexStatus::InitFailed => {
            "SQLite failed to initialize when diskcache_oxide loaded; each open will retry, and fail \
             with SQLite's error if it still can't"
        }
    };
    // A failure to log must not fail the import: the status is still readable through the hook.
    let log = py.import("logging").and_then(|logging| logging.call_method1("getLogger", ("diskcache_oxide",)));
    if let Ok(log) = log {
        let _ = log.call_method1("warning", (message,));
    }
    Ok(())
}

/// The crate version in the PEP 440 form the wheel's metadata has. maturin derives the wheel's
/// version from Cargo's, turning a semver pre-release such as `0.1.0-dev.1` into `0.1.0.dev1`; this
/// does the same, so `__version__` matches what pip and `importlib.metadata` report
/// (`tests/unit/test_version.py`), including a tag with no number (`-alpha` is `a0`). maturin
/// refuses to build any other tag, so the fallback below never ships.
fn pep440_version() -> String {
    pep440(env!("CARGO_PKG_VERSION"))
}

fn pep440(version: &str) -> String {
    let Some((release, pre)) = version.split_once('-') else { return version.to_owned() };
    let (tag, number) = pre.split_once('.').unwrap_or((pre, "0"));
    let tag = match tag {
        "alpha" => "a",
        "beta" => "b",
        "rc" | "pre" | "c" | "preview" => "rc",
        "dev" => ".dev",
        _ => return version.to_owned(),
    };
    format!("{release}{tag}{number}")
}

#[pymodule(gil_used = false)]
fn _core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    let py = m.py();
    // First, before anything here can reach SQLite (`_sqlite_memory` takes its MEM static mutex).
    install_sqlite_mutex(py)?;
    // Only in a spin-sweep benchmark build (plan 12 step 5): the budget the sweep's override set.
    #[cfg(dco_spin_sweep)]
    m.add("_SWEEP_SPIN_BUDGET", sqlite_mutex::sweep_spin_budget())?;
    // And the slot wait's spin budget and spinner limit (5b).
    #[cfg(dco_spin_sweep)]
    m.add("_SWEEP_SLOT_WAIT", diskcache_oxide_core::genlock::sweep_slot_wait())?;
    m.add("__version__", pep440_version())?;
    m.add("BUNDLED_SQLITE_VERSION", sqlite::compile_version())?;
    // Lets benchmarks refuse to measure an unoptimized build.
    m.add("_DEBUG_BUILD", cfg!(debug_assertions))?;
    m.add("Timeout", py.get_type::<Timeout>())?;
    m.add("TransactionBusy", py.get_type::<TransactionBusy>())?;
    m.add("TransactionAborted", py.get_type::<TransactionAborted>())?;
    m.add("UnsafeValueError", py.get_type::<UnsafeValueError>())?;
    m.add("CorruptValueError", py.get_type::<CorruptValueError>())?;
    m.add("ForeignValueError", py.get_type::<ForeignValueError>())?;
    m.add_function(wrap_pyfunction!(sqlite_version, m)?)?;
    #[cfg(coverage)]
    m.add_function(wrap_pyfunction!(_write_coverage, m)?)?;
    m.add_function(wrap_pyfunction!(sqlite_compile_options, m)?)?;
    // Test hooks for the value codec; not part of the public API.
    m.add_function(wrap_pyfunction!(codec::_encode, m)?)?;
    m.add_function(wrap_pyfunction!(codec::_decode, m)?)?;
    m.add_function(wrap_pyfunction!(_sqlite_memory, m)?)?;
    m.add_function(wrap_pyfunction!(_gate_stats, m)?)?;
    m.add_function(wrap_pyfunction!(cache::_open_fanout_keeping_stored, m)?)?;
    m.add_function(wrap_pyfunction!(_sqlite_error, m)?)?;
    m.add_function(wrap_pyfunction!(_held_stores, m)?)?;
    m.add_function(wrap_pyfunction!(_sqlite_mutex_status, m)?)?;
    m.add("FormatMismatchError", py.get_type::<FormatMismatchError>())?;
    m.add("LayoutMismatchError", layout_mismatch_type(py)?)?;
    m.add_class::<cache::Cache>()?;
    m.add_class::<cache::FanoutCache>()?;
    m.add_class::<cache::Transaction>()?;
    m.add_class::<codec::TypeRegistry>()?;
    m.add("registry", codec::registry::global(py)?)?;
    install_fork_barrier(m)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::pep440;

    /// maturin's mapping of Cargo pre-release tags; `tests/unit/test_version.py` only sees the
    /// version being built, so every tag is pinned here.
    #[test]
    fn pep440_matches_maturin() {
        for (cargo, wheel) in [
            ("0.1.0", "0.1.0"),
            ("0.1.0-dev.1", "0.1.0.dev1"),
            ("0.1.0-dev", "0.1.0.dev0"),
            ("1.2.3-alpha.4", "1.2.3a4"),
            ("1.2.3-alpha", "1.2.3a0"),
            ("1.2.3-beta.2", "1.2.3b2"),
            ("1.2.3-rc.1", "1.2.3rc1"),
            ("1.2.3-pre.1", "1.2.3rc1"),
            ("1.2.3-c.1", "1.2.3rc1"),
            ("1.2.3-preview.5", "1.2.3rc5"),
            ("1.2.3-nightly.1", "1.2.3-nightly.1"),
        ] {
            assert_eq!(pep440(cargo), wheel, "{cargo}");
        }
    }
}
