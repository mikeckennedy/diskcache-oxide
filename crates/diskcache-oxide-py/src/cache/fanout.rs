//! `diskcache_oxide.FanoutCache`: one handle over the N shard stores of a fan-out root (plan 13).
//!
//! Each key lives in exactly one shard, chosen by the frozen hash in `core::fanout`, so every
//! process routes it the same way forever. A routed operation is exactly `Cache`'s, run by the same
//! `HandleConfig` method against the key's shard. Calls over every shard (`len`, `volume`, `close`)
//! share one deadline, not one each. Fan-out maintenance and iteration come in later steps.

use std::path::PathBuf;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::{Duration, Instant};

use diskcache_oxide_core::fanout::{self, Manifest};
use diskcache_oxide_core::open::Role;
use diskcache_oxide_core::ops::Number;
use diskcache_oxide_core::store::Store;
use diskcache_oxide_core::{Error, lockwait};
use pyo3::PyTraverseError;
use pyo3::exceptions::{PyKeyError, PyValueError};
use pyo3::gc::PyVisit;
use pyo3::prelude::*;

use super::{DecodePolicy, Delta, HandleConfig, IncrDefault, Settings, StoreHandle, str_key, whole};
use crate::to_py_err;

/// A disk cache split over several SQLite databases (shards), so reads and writes on different
/// keys don't share one file's locks.
#[pyclass(frozen, module = "diskcache_oxide")]
pub(crate) struct FanoutCache {
    /// One per shard, in shard order: `shards[i].shard == i`.
    shards: Box<[StoreHandle]>,
    config: HandleConfig,
}

#[pymethods]
impl FanoutCache {
    #[new]
    #[pyo3(signature = (
        directory,
        timeout = None,
        *,
        shards = None,
        size_limit = None,
        cull_limit = None,
        serializer = "msgpack",
        pickle = false,
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
        shards: Option<&Bound<'_, PyAny>>,
        size_limit: Option<&Bound<'_, PyAny>>,
        cull_limit: Option<&Bound<'_, PyAny>>,
        serializer: &str,
        pickle: bool,
        clock: Option<Py<PyAny>>,
        max_connections: Option<&Bound<'_, PyAny>>,
        sqlite_cache_size: Option<&Bound<'_, PyAny>>,
        wal_size_limit: Option<&Bound<'_, PyAny>>,
        wal_checkpoint: &str,
        on_decode_error: Option<DecodePolicy>,
        durable: bool,
    ) -> PyResult<Self> {
        let settings = Settings::parse(
            py,
            timeout,
            size_limit,
            cull_limit,
            serializer,
            max_connections,
            sqlite_cache_size,
            wal_size_limit,
            wal_checkpoint,
        )?;
        let shards = shard_count(shards)?;
        let stores = crate::detach(py, || open(&directory, &settings, shards))
            .map_err(|(e, shards)| open_error(py, e, shards))?;
        let shards = stores
            .into_iter()
            .enumerate()
            .map(|(shard, store)| StoreHandle { store, shard, closed: AtomicBool::new(false) })
            .collect();
        Ok(Self { shards, config: HandleConfig::new(&directory, &settings, pickle, clock, on_decode_error, durable) })
    }

    // -- attributes ---------------------------------------------------------------------------

    /// The root directory.
    #[getter]
    fn directory(&self) -> &str {
        &self.config.directory
    }

    #[getter]
    fn timeout(&self) -> f64 {
        self.config.timeout.as_secs_f64()
    }

    /// How many shards the root has, as its manifest records.
    #[getter]
    fn shards(&self) -> usize {
        self.shards.len()
    }

    /// The sum of every shard's stored `size_limit`.
    #[getter]
    fn size_limit(&self) -> i64 {
        self.shards.iter().map(|h| h.store.cached_limits().size_limit).sum()
    }

    /// Shard 0's stored `cull_limit`. Every shard is opened with the same one, so this is every
    /// shard's only while they were all created, and last set, under the same settings.
    #[getter]
    fn cull_limit(&self) -> i64 {
        self.shards[0].store.cached_limits().cull_limit
    }

    /// Always True for a real cache (a `NullCache` is False).
    #[getter]
    #[allow(clippy::unused_self)] // A Python attribute: it needs `self` even though it's constant.
    fn enabled(&self) -> bool {
        true
    }

    /// The shard `key` lives in, `0 <= shard_of(key) < shards`: the same in every process and every
    /// release. Keys with the same shard can share a `transact(key=...)`.
    fn shard_of(&self, key: &Bound<'_, PyAny>) -> PyResult<usize> {
        Ok(self.route(str_key(key)?).shard)
    }

    /// Test hook: every failure `durable=True` has swallowed on this handle, as `Cache`'s.
    fn _durable_stats<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, pyo3::types::PyDict>> {
        match &self.config.failures {
            Some(failures) => failures.stats(py),
            None => Ok(pyo3::types::PyDict::new(py)),
        }
    }

    /// Test hook: how many open handles each shard's store counts, in shard order.
    fn _handle_counts(&self) -> Vec<isize> {
        self.shards.iter().map(|h| h.store.handles()).collect()
    }

    fn __repr__(&self) -> String {
        format!("FanoutCache({:?}, shards={})", self.config.directory, self.shards.len())
    }

    fn __traverse__(&self, visit: PyVisit<'_>) -> Result<(), PyTraverseError> {
        self.config.traverse(&visit)
    }

    // -- routed to one shard -------------------------------------------------------------------

    #[pyo3(signature = (key, default = None, *, expire_time = false))]
    fn get(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        default: Option<Py<PyAny>>,
        expire_time: bool,
    ) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        self.config.get(py, self.route(name), name, default, expire_time)
    }

    fn __getitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        self.config.get_item(py, self.route(name), key, name)
    }

    fn __contains__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        let name = str_key(key)?;
        self.config.contains(py, self.route(name), name)
    }

    #[pyo3(signature = (key, value, expire = None))]
    fn set(
        &self,
        py: Python<'_>,
        key: &Bound<'_, PyAny>,
        value: &Bound<'_, PyAny>,
        expire: Option<&Bound<'_, PyAny>>,
    ) -> PyResult<bool> {
        let name = str_key(key)?;
        self.config.set(py, self.route(name), name, value, expire)
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
        let name = str_key(key)?;
        self.config.add(py, self.route(name), name, value, expire)
    }

    fn delete(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<bool> {
        let name = str_key(key)?;
        self.config.delete(py, self.route(name), name)
    }

    fn __delitem__(&self, py: Python<'_>, key: &Bound<'_, PyAny>) -> PyResult<()> {
        if self.delete(py, key)? { Ok(()) } else { Err(PyKeyError::new_err(key.clone().unbind())) }
    }

    #[pyo3(signature = (key, default = None))]
    fn pop(&self, py: Python<'_>, key: &Bound<'_, PyAny>, default: Option<Py<PyAny>>) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        self.config.pop(py, self.route(name), name, default)
    }

    #[pyo3(signature = (key, expire = None))]
    fn touch(&self, py: Python<'_>, key: &Bound<'_, PyAny>, expire: Option<&Bound<'_, PyAny>>) -> PyResult<bool> {
        let name = str_key(key)?;
        self.config.touch(py, self.route(name), name, expire)
    }

    #[pyo3(signature = (key, delta = Delta(Number::Int(1)), default = IncrDefault(Some(Number::Int(0)))))]
    fn incr(&self, py: Python<'_>, key: &Bound<'_, PyAny>, delta: Delta, default: IncrDefault) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        self.config.add_number(py, self.route(name), key, name, delta, default, false)
    }

    #[pyo3(signature = (key, delta = Delta(Number::Int(1)), default = IncrDefault(Some(Number::Int(0)))))]
    fn decr(&self, py: Python<'_>, key: &Bound<'_, PyAny>, delta: Delta, default: IncrDefault) -> PyResult<Py<PyAny>> {
        let name = str_key(key)?;
        self.config.add_number(py, self.route(name), key, name, delta, default, true)
    }

    // -- over every shard, under one deadline ---------------------------------------------------

    fn __len__(&self, py: Python<'_>) -> PyResult<usize> {
        let counts = self.every_shard(py, Store::len)?;
        Ok(counts.into_iter().map(|n| usize::try_from(n).unwrap_or(0)).sum())
    }

    /// Always True: a cache is usable even when empty (D-009).
    #[allow(clippy::unused_self)] // A Python protocol method: it needs `self`.
    fn __bool__(&self) -> bool {
        true
    }

    /// The sum of every shard's volume.
    fn volume(&self, py: Python<'_>) -> PyResult<i64> {
        Ok(self.every_shard(py, Store::volume)?.into_iter().sum())
    }

    // -- lifecycle ----------------------------------------------------------------------------

    /// Releases this handle on every shard, as `Cache.close()` does for its one store, but with
    /// one budget for the whole close: the checkpoints of large WALs share at most 1 s (or
    /// `timeout`, if shorter), and behind a pending fork every release together waits at most 1 s
    /// before it's left for the end of the fork (D-027). Never raises. Using the cache again
    /// reopens the shards it touches. Closing twice is harmless.
    #[pyo3(signature = (*, checkpoint = true))]
    fn close(&self, py: Python<'_>, checkpoint: bool) {
        if let Some(failures) = &self.config.failures {
            failures.flush(py, &self.config.directory);
        }
        let open: Vec<&std::sync::Arc<Store>> =
            self.shards.iter().filter(|h| !h.closed.swap(true, Ordering::AcqRel)).map(|h| &h.store).collect();
        if open.is_empty() {
            return;
        }
        let budget = checkpoint.then(|| self.config.timeout.min(Duration::from_secs(1)));
        // Released only when some shard's last handle closes on a large WAL, as in `Cache.close()`.
        if open.iter().any(|s| s.closing_on_a_large_wal()) {
            crate::detach(py, || Store::close_all(&open, budget));
        } else {
            Store::close_all(&open, budget);
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
}

impl FanoutCache {
    /// The handle of the shard `key` hashes to.
    #[inline]
    fn route(&self, key: &str) -> &StoreHandle {
        // The shard count came from a validated manifest, so it fits a `u32`.
        let count = u32::try_from(self.shards.len()).unwrap_or(u32::MAX);
        &self.shards[fanout::shard_of_str(key, count)]
    }

    /// `call` on every shard in order, all under one deadline of the handle's `timeout` (D-027's
    /// one budget, not one each): a closed shard rejoins first, out of the same budget. The first
    /// failure raises, as admin calls do under `durable` (D-028); a timeout names the whole budget.
    fn every_shard<T>(&self, py: Python<'_>, call: impl Fn(&Store, Duration) -> Result<T, Error>) -> PyResult<Vec<T>> {
        let timeout = self.config.timeout;
        let deadline = lockwait::deadline_after(Instant::now(), timeout);
        let mut results = Vec::with_capacity(self.shards.len());
        for handle in &self.shards {
            let left = deadline.saturating_duration_since(Instant::now());
            let result = handle.ensure_open(left).and_then(|left| call(&handle.store, left));
            results.push(result.map_err(|error| match error {
                Error::Timeout { .. } => to_py_err(Error::Timeout { seconds: timeout.as_secs_f64() }),
                other => to_py_err(other),
            })?);
            py.check_signals()?;
        }
        Ok(results)
    }
}

impl Drop for FanoutCache {
    /// A cache dropped without `close()`: releases every shard still open, as `close()` does but
    /// with no checkpoint, detached on a large WAL as `Cache`'s drop is.
    fn drop(&mut self) {
        let open: Vec<&std::sync::Arc<Store>> =
            self.shards.iter().filter(|h| !h.closed.swap(true, Ordering::AcqRel)).map(|h| &h.store).collect();
        if open.is_empty() {
            return;
        }
        let detached = open.iter().any(|s| s.closing_on_a_large_wal())
            && Python::try_attach(|py| crate::detach(py, || Store::close_all(&open, None))).is_some();
        if !detached {
            Store::close_all(&open, None);
        }
    }
}

/// About how many file descriptors opening one shard takes: its pin, the connection the open
/// seeds the pool with (the database and its `-wal`), and the shared `-shm`. Each further pooled
/// connection a shard opens in use adds two more.
const FDS_PER_SHARD: u64 = 4;

/// The exception a failed open raises. An I/O failure opening a shard when this process can't
/// have that many more files open (`ulimit -n`) is almost always that limit, which SQLite reports
/// only as "unable to open database file": say so, with the numbers, and keep SQLite's as the
/// cause. The check runs after the failed open has released its shards, so it compares the
/// limit with what is open now plus what the shards need, not with a count taken mid-failure.
fn open_error(py: Python<'_>, error: Error, shards: Option<u32>) -> PyErr {
    let Some(message) = shards.and_then(|shards| fd_shortfall(&error, shards)) else { return to_py_err(error) };
    let raised = pyo3::exceptions::PyOSError::new_err((libc::EMFILE, message));
    raised.set_cause(py, Some(to_py_err(error)));
    raised
}

/// The message for an open that failed for want of file descriptors, or `None` if that's not it.
fn fd_shortfall(error: &Error, shards: u32) -> Option<String> {
    let io = match error {
        Error::SqliteIo { .. } => true,
        Error::Io { source, .. } => matches!(source.raw_os_error(), Some(libc::EMFILE | libc::ENFILE)),
        _ => false,
    };
    if !io {
        return None;
    }
    let soft = soft_fd_limit()?;
    let open = open_fds()?;
    let need = FDS_PER_SHARD * u64::from(shards);
    (open + need > soft).then(|| {
        format!(
            "a FanoutCache with {shards} shards needs about {need} file descriptors to open ({FDS_PER_SHARD} per \
             shard, and 2 more per connection each shard opens in use), but this process may have only {soft} \
             open (its soft RLIMIT_NOFILE, `ulimit -n`) and already has {open}; use fewer shards, or raise the \
             limit (`ulimit -n`, or `resource.setrlimit(resource.RLIMIT_NOFILE, ...)`)"
        )
    })
}

/// This process's soft limit on open files, if it has a finite one.
#[allow(unsafe_code)]
fn soft_fd_limit() -> Option<u64> {
    let mut limit = libc::rlimit { rlim_cur: 0, rlim_max: 0 };
    // SAFETY: `getrlimit` only writes the struct it is given, which lives for the call.
    let ok = unsafe { libc::getrlimit(libc::RLIMIT_NOFILE, &raw mut limit) } == 0;
    (ok && limit.rlim_cur != libc::RLIM_INFINITY).then_some(limit.rlim_cur)
}

/// How many file descriptors this process has open now (its `/dev/fd` entries).
fn open_fds() -> Option<u64> {
    let dir = if cfg!(target_os = "linux") { "/proc/self/fd" } else { "/dev/fd" };
    std::fs::read_dir(dir).ok().map(|entries| entries.count() as u64)
}

/// The `shards=` argument: `None` (the stored count, or 8 for a new root) or an int from 1 to 256.
fn shard_count(shards: Option<&Bound<'_, PyAny>>) -> PyResult<Option<u32>> {
    let Some(n) = whole("shards", shards)? else { return Ok(None) };
    match u32::try_from(n) {
        Ok(n) if (fanout::MIN_SHARDS..=fanout::MAX_SHARDS).contains(&n) => Ok(Some(n)),
        _ => Err(PyValueError::new_err(format!(
            "shards must be between {} and {}, got {n}",
            fanout::MIN_SHARDS,
            fanout::MAX_SHARDS
        ))),
    }
}

/// Opens the root's manifest, then every shard under one deadline of the handle's `timeout`. With
/// no `max_connections`, the total is one per shard, and never fewer than a plain cache's default.
/// A failure comes with the shard count once the manifest has given it, for [`open_error`].
fn open(
    root: &std::path::Path,
    settings: &Settings,
    shards: Option<u32>,
) -> Result<Vec<std::sync::Arc<Store>>, (Error, Option<u32>)> {
    let deadline = lockwait::deadline_after(Instant::now(), settings.timeout);
    let manifest: Manifest = fanout::open_manifest(root, shards).map_err(|e| (e, None))?;
    let total = settings.max_connections.unwrap_or_else(|| Store::default_total_connections(manifest.shards()));
    // The role is set per shard by `open_shards`.
    let base = settings.options(root.to_path_buf(), total, Role::Plain);
    Store::open_shards(&base, manifest, deadline).map_err(|error| match error {
        Error::Timeout { .. } => (Error::Timeout { seconds: settings.timeout.as_secs_f64() }, None),
        other => (other, Some(manifest.shards())),
    })
}
